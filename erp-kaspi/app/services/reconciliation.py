"""Bank reconciliation matcher v1 (G-BANK-5).

Deterministic, no ML. Layers (docs/RECONCILIATION_FRAMEWORK_2026-10-04.md):

* Layer 1 — external fact: ``bank_statement_lines`` (immutable).
* Layer 2 — this matcher: relationship assertions line <-> operation.
* Layer 3 — ledger: operations/transfers/settlement_links (untouched here).

Hard gates (every rule): same ``ip_id``; ``operations.status = 'POSTED'``;
the operation is not already CONFIRMED-matched to another line (partial
unique index ``uq_bank_statement_lines_confirmed_op``); the line itself is
UNMATCHED.

Verdicts are recorded in ``provider_extra["audit"]`` — the only mutable
sub-key of ``provider_extra`` (the immutability trigger freezes every
other key). ``SUGGESTED``/``CONFIRMED`` always carry ``matched_operation_id``
(DB CHECK); verdicts without a single operation (TRANSFER_PAIR,
COMMISSION_SUGGESTION, MISMATCH, NEEDS_REVIEW) stay UNMATCHED with the
verdict in the audit trail.

Forbidden: auto-CONFIRMED by the machine (only post_unmatched_as_operations
may CONFIRM, for operations it created itself); MATCH on amount alone.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.bank import BankStatementLine
from app.models.finance import Operation
from app.models.reference import Counterparty
from app.services.bank_import import PostingPolicy, commission_knp_codes

#: Rule version recorded with every suggestion (threshold changes are traceable).
MATCHER_RULE_VERSION = "v1"

Verdict = Literal[
    "EXACT_REFERENCE_MATCH",
    "PARTIAL_MATCH",
    "SCORED_CANDIDATE",
    "TRANSFER_PAIR",
    "COMMISSION_SUGGESTION",
    "MISMATCH",
    "NEEDS_REVIEW",
]


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(line: BankStatementLine, verdict: Verdict, **extra) -> None:
    """Append a verdict to provider_extra['audit'] (the mutable sub-key)."""
    current = dict(line.provider_extra or {})
    audit = dict(current.get("audit") or {})
    audit.update({"verdict": verdict, "rule_version": MATCHER_RULE_VERSION, **extra})
    current["audit"] = audit
    line.provider_extra = current


def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]", "", name.lower())


class BankReconciliationService:
    """Deterministic matcher v1 + human workflow transitions."""

    # -- matcher ------------------------------------------------------- #

    def suggest_matches(
        self,
        session: Session,
        *,
        ip_id: int,
        statement_id: int | None = None,
        account_id: int | None = None,
        policy: PostingPolicy | None = None,
    ) -> dict[str, int]:
        """Run rules R1..R4 over UNMATCHED lines. Idempotent: re-running
        never re-suggests (only UNMATCHED lines are touched)."""
        policy = policy or PostingPolicy()
        knp_codes = commission_knp_codes(session, policy)
        query = select(BankStatementLine).where(
            BankStatementLine.ip_id == ip_id,
            BankStatementLine.match_status == "UNMATCHED",
        )
        if statement_id is not None:
            query = query.where(BankStatementLine.statement_id == statement_id)
        if account_id is not None:
            query = query.where(BankStatementLine.account_id == account_id)
        lines = session.execute(query.order_by(BankStatementLine.id)).scalars().all()

        counts = {
            "suggested": 0,
            "transfer_pair": 0,
            "commission": 0,
            "mismatch": 0,
            "needs_review": 0,
            "untouched": 0,
        }
        for line in lines:
            outcome = self._suggest_one(session, line, policy, knp_codes)
            counts[outcome] += 1
        session.flush()  # §9: caller owns the transaction; flush only
        return counts

    def _suggest_one(
        self,
        session: Session,
        line: BankStatementLine,
        policy: PostingPolicy,
        knp_codes: frozenset[str],
    ) -> str:
        # R3 — bank commission: its own economic event, never matched to an
        # existing operation. post_unmatched_as_operations creates EXPENSE_BANK_FEE.
        if line.direction == "OUT" and line.knp_code and line.knp_code in knp_codes:
            _audit(line, "COMMISSION_SUGGESTION", knp_code=line.knp_code)
            return "commission"
        # R1 — exact reference (bank_reference / document_number = op.external_id).
        outcome = self._rule_reference(session, line, policy)
        if outcome is not None:
            return outcome
        # R4 — scored candidate (no reference): amount+direction+account+date
        # window are gates; counterparty corroborates; amount alone never matches.
        outcome = self._rule_scored(session, line, policy)
        if outcome is not None:
            return outcome
        # R2 — inter-account transfer pair (no operations are created).
        pair_id = self._find_transfer_pair(session, line, policy)
        if pair_id is not None:
            _audit(line, "TRANSFER_PAIR", pair_line_id=pair_id)
            return "transfer_pair"
        _audit(line, "NEEDS_REVIEW", candidates=[])
        return "needs_review"

    # -- rule R1 -------------------------------------------------------- #

    def _candidate_ops(self, session: Session, line: BankStatementLine):
        """POSTED ops of the same ИП, account-consistent, not CONFIRMED-matched."""
        confirmed = select(BankStatementLine.matched_operation_id).where(
            BankStatementLine.match_status == "CONFIRMED",
            BankStatementLine.matched_operation_id.is_not(None),
        )
        query = select(Operation).where(
            Operation.ip_id == line.ip_id,
            Operation.status == "POSTED",
            ~Operation.id.in_(confirmed),
        )
        if line.account_id is None:
            query = query.where(Operation.account_id.is_(None))
        else:
            query = query.where(Operation.account_id == line.account_id)
        # Direction consistency: OUT lines are negative ops, IN positive (§27).
        if line.direction == "OUT":
            query = query.where(Operation.amount < 0)
        else:
            query = query.where(Operation.amount > 0)
        return query

    def _rule_reference(
        self, session: Session, line: BankStatementLine, policy: PostingPolicy
    ) -> str | None:
        refs = {r for r in (line.bank_reference, line.document_number) if r}
        if not refs:
            return None
        candidates = (
            session.execute(
                self._candidate_ops(session, line).where(Operation.external_id.in_(refs))
            )
            .scalars()
            .all()
        )
        if not candidates:
            return None
        if len(candidates) > 1:
            _audit(
                line,
                "NEEDS_REVIEW",
                candidates=[op.id for op in candidates],
                reason="multiple reference candidates",
            )
            return "needs_review"
        op = candidates[0]
        amount_delta = abs(abs(op.amount) - line.amount)
        date_delta = abs((op.operation_date - line.value_date).days)
        if amount_delta == 0 and date_delta <= policy.date_window_days:
            self._suggest(session, line, op, "EXACT_REFERENCE_MATCH")
            return "suggested"
        if amount_delta != 0 and amount_delta <= policy.amount_tolerance:
            # PARTIAL: identity established, amount within tolerance (bank fee
            # netted in-line, rounding). Human accepts the Δ.
            self._suggest(
                session,
                line,
                op,
                "PARTIAL_MATCH",
                partial_kind="amount",
                amount_delta=str(amount_delta),
            )
            return "suggested"
        if amount_delta == 0 and date_delta <= _MAX_DRIFT_DAYS:
            # PARTIAL: reference + amount exact, date drifted (late settlement).
            self._suggest(
                session,
                line,
                op,
                "PARTIAL_MATCH",
                partial_kind="date",
                date_delta_days=date_delta,
            )
            return "suggested"
        # MISMATCH: reference matches but the fact contradicts (amount off
        # beyond tolerance, or date beyond max drift). Suggestion is BLOCKED.
        _audit(
            line,
            "MISMATCH",
            operation_id=op.id,
            amount_delta=str(amount_delta),
            date_delta_days=date_delta,
        )
        return "mismatch"

    # -- rule R4 -------------------------------------------------------- #

    def _rule_scored(
        self, session: Session, line: BankStatementLine, policy: PostingPolicy
    ) -> str | None:
        """Scored candidate without a reference (framework R4).

        Gates (all required): exact amount, direction, account, operation
        date within the window, AND counterparty corroboration (IIN exact
        or normalized-name match). Amount alone never suggests (T4).
        """
        start = _shift_days(line.value_date, -policy.date_window_days)
        end = _shift_days(line.value_date, policy.date_window_days)
        gate_rows = session.execute(
            self._candidate_ops(session, line)
            .where(
                func.abs(Operation.amount) == line.amount,
                Operation.operation_date.between(start, end),
            )
            .outerjoin(Counterparty, Counterparty.id == Operation.counterparty_id)
            .add_columns(Counterparty.bin_iin, Counterparty.name)
        ).all()
        if not gate_rows:
            return None
        scored: list[tuple[Decimal, Operation]] = []
        weak: list[int] = []
        for op, cp_iin, cp_name in gate_rows:
            corroborated, cp_score = self._corroborate(line, cp_iin, cp_name)
            if not corroborated:
                weak.append(op.id)
                continue
            score = Decimal("40") + Decimal("20") + cp_score  # amount + account + cp
            date_delta = abs((op.operation_date - line.value_date).days)
            if policy.date_window_days:
                score += Decimal("15") * (
                    Decimal(policy.date_window_days - date_delta) / Decimal(policy.date_window_days)
                )
            else:
                score += Decimal("15")
            scored.append((score, op))
        if not scored:
            # Gate-passing ops exist but no counterparty corroboration:
            # NEVER suggest on amount alone (T4) — list them for review.
            _audit(line, "NEEDS_REVIEW", candidates=[{"operation_id": i} for i in weak])
            return "needs_review"
        if len(scored) > 1:
            _audit(
                line,
                "NEEDS_REVIEW",
                candidates=[{"operation_id": op.id, "score": str(s)} for s, op in scored],
                reason="ambiguous: several corroborated candidates",
            )
            return "needs_review"
        best_score, best_op = scored[0]
        if best_score >= policy.suggest_threshold:
            self._suggest(session, line, best_op, "SCORED_CANDIDATE", score=str(best_score))
            return "suggested"
        _audit(
            line,
            "NEEDS_REVIEW",
            candidates=[{"operation_id": best_op.id, "score": str(best_score)}],
            reason="below suggest_threshold",
        )
        return "needs_review"

    @staticmethod
    def _corroborate(
        line: BankStatementLine, cp_iin: str | None, cp_name: str | None
    ) -> tuple[bool, Decimal]:
        """Counterparty corroboration: IIN exact (strong, 15) or normalized
        name (weak, 10). Both sides must be present — absent data is not
        corroboration."""
        if line.counterparty_iin and cp_iin and line.counterparty_iin == cp_iin:
            return True, Decimal("15")
        line_name = _normalize_name(line.counterparty_name)
        op_name = _normalize_name(cp_name)
        if line_name and op_name and line_name == op_name:
            return True, Decimal("10")
        return False, Decimal("0")

    # -- rule R2 -------------------------------------------------------- #

    def _find_transfer_pair(
        self, session: Session, line: BankStatementLine, policy: PostingPolicy
    ) -> int | None:
        """OUT+IN, same amount, ±1 day, different provider accounts, same ИП."""
        opposite = "IN" if line.direction == "OUT" else "OUT"
        return session.execute(
            select(BankStatementLine.id).where(
                BankStatementLine.ip_id == line.ip_id,
                BankStatementLine.id != line.id,
                BankStatementLine.direction == opposite,
                BankStatementLine.match_status == "UNMATCHED",
                BankStatementLine.provider_account != line.provider_account,
                func.abs(BankStatementLine.value_date - line.value_date) <= 1,
                func.abs(BankStatementLine.amount - line.amount) <= policy.amount_tolerance,
            )
        ).scalar_one_or_none()

    # -- workflow transitions ------------------------------------------- #

    def _suggest(
        self, session: Session, line: BankStatementLine, op: Operation, verdict: Verdict, **extra
    ) -> None:
        line.match_status = "SUGGESTED"
        line.matched_operation_id = op.id
        _audit(line, verdict, operation_id=op.id, **extra)

    def confirm_line(
        self, session: Session, *, ip_id: int, line_id: int, operation_id: int, user_id: int | None
    ) -> BankStatementLine:
        """Human confirm: SUGGESTED/UNMATCHED -> CONFIRMED (row-locked)."""
        line = session.execute(
            select(BankStatementLine)
            .where(BankStatementLine.id == line_id, BankStatementLine.ip_id == ip_id)
            .with_for_update()
        ).scalar_one_or_none()
        if line is None:
            raise ValueError(f"bank statement line {line_id} not found for ip {ip_id}")
        if line.match_status not in ("SUGGESTED", "UNMATCHED"):
            raise ValueError(
                f"line {line_id} is {line.match_status}; only SUGGESTED/UNMATCHED can be confirmed"
            )
        op = session.get(Operation, operation_id)
        if op is None or op.ip_id != ip_id:
            raise ValueError(f"operation {operation_id} not found for ip {ip_id}")
        if op.status != "POSTED":
            raise ValueError("only POSTED operations can be matched")
        taken = session.execute(
            select(func.count())
            .select_from(BankStatementLine)
            .where(
                BankStatementLine.matched_operation_id == operation_id,
                BankStatementLine.match_status == "CONFIRMED",
                BankStatementLine.id != line_id,
            )
        ).scalar_one()
        if taken:
            raise ValueError(f"operation {operation_id} is already CONFIRMED-matched")
        line.match_status = "CONFIRMED"
        line.matched_operation_id = operation_id
        line.matched_at = _now()
        line.matched_by = user_id
        session.flush()
        return line

    def ignore_line(
        self, session: Session, *, ip_id: int, line_id: int, reason: str, user_id: int | None
    ) -> BankStatementLine:
        """Human ignore with a mandatory reason (UNMATCHED/SUGGESTED -> IGNORED)."""
        if not reason or len(reason.strip()) < 10:
            raise ValueError("ignore reason is required (min 10 characters)")
        line = session.get(BankStatementLine, line_id)
        if line is None or line.ip_id != ip_id:
            raise ValueError(f"bank statement line {line_id} not found for ip {ip_id}")
        if line.match_status not in ("UNMATCHED", "SUGGESTED"):
            raise ValueError(f"line {line_id} is {line.match_status}; cannot ignore")
        line.match_status = "IGNORED"
        line.matched_operation_id = None
        _audit(
            line,
            line.provider_extra.get("audit", {}).get("verdict", "NEEDS_REVIEW"),
            ignore_reason=reason.strip(),
            ignored_by=user_id,
        )
        session.flush()
        return line

    def reject_suggestion(
        self, session: Session, *, ip_id: int, line_id: int, user_id: int | None
    ) -> BankStatementLine:
        """Human reject: SUGGESTED -> UNMATCHED (re-eligible for the matcher)."""
        line = session.get(BankStatementLine, line_id)
        if line is None or line.ip_id != ip_id:
            raise ValueError(f"bank statement line {line_id} not found for ip {ip_id}")
        if line.match_status != "SUGGESTED":
            raise ValueError(f"line {line_id} is {line.match_status}; nothing to reject")
        line.match_status = "UNMATCHED"
        line.matched_operation_id = None
        _audit(line, "NEEDS_REVIEW", rejected_by=user_id, reason="suggestion rejected")
        session.flush()
        return line

    def reopen_line(
        self, session: Session, *, ip_id: int, line_id: int, reason: str, user_id: int | None
    ) -> BankStatementLine:
        """Human reopen: IGNORED/CONFIRMED -> UNMATCHED (reason required)."""
        if not reason or len(reason.strip()) < 10:
            raise ValueError("reopen reason is required (min 10 characters)")
        line = session.get(BankStatementLine, line_id)
        if line is None or line.ip_id != ip_id:
            raise ValueError(f"bank statement line {line_id} not found for ip {ip_id}")
        if line.match_status not in ("IGNORED", "CONFIRMED"):
            raise ValueError(f"line {line_id} is {line.match_status}; cannot reopen")
        line.match_status = "UNMATCHED"
        line.matched_operation_id = None
        _audit(line, "NEEDS_REVIEW", reopened_by=user_id, reopen_reason=reason.strip())
        session.flush()
        return line


_MAX_DRIFT_DAYS = 7


def _shift_days(value, days: int):
    from datetime import timedelta

    return value + timedelta(days=days)


# Re-exported for the API layer.
__all__ = [
    "MATCHER_RULE_VERSION",
    "BankReconciliationService",
    "Verdict",
]
