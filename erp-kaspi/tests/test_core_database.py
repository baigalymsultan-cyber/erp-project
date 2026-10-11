"""
Tests for app/core/config.py and app/core/database.py.

MASTER PROMPT references
------------------------
§10 — credentials come from the environment and must never be logged; the
      masking helper is therefore a security control and is tested as one.
§15 — every connection works in UTC; TIMESTAMPTZ values are stored in UTC.
§54 — audit rows carry the acting user through the session variable
      app.current_user_id, which the database trigger fn_audit() reads. The
      contract between Python and the trigger is verified end to end here,
      because a silent mismatch would produce an audit trail with NULL users.
§90 — every guarantee is verified by an automated check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import (
    ConfigurationError,
    DatabaseSettings,
    Secret,
    get_database_settings,
)
from app.core.database import set_current_user

# ---------------------------------------------------------------------------
# 1. DatabaseSettings.from_env — validation
# ---------------------------------------------------------------------------

_MINIMAL_ENV = {
    "DATABASE_HOST": "db.internal",
    "DATABASE_PORT": "5433",
    "DATABASE_NAME": "erp_prod",
    "DATABASE_USER": "erp_app",
    "DATABASE_PASSWORD": "s3cret",
}


def test_from_env_reads_every_field() -> None:
    settings = DatabaseSettings.from_env(dict(_MINIMAL_ENV))
    assert settings.host == "db.internal"
    assert settings.port == 5433
    assert settings.name == "erp_prod"
    assert settings.user == "erp_app"
    assert settings.password.reveal() == "s3cret"


def test_from_env_applies_defaults_for_optional_fields() -> None:
    settings = DatabaseSettings.from_env(dict(_MINIMAL_ENV))
    assert settings.statement_timeout_ms == 30_000
    assert settings.application_name == "erp-backend"


def test_from_env_honours_explicit_timeout_and_app_name() -> None:
    env = dict(_MINIMAL_ENV) | {
        "DATABASE_STATEMENT_TIMEOUT_MS": "5000",
        "DATABASE_APP_NAME": "erp-etl",
    }
    settings = DatabaseSettings.from_env(env)
    assert settings.statement_timeout_ms == 5000
    assert settings.application_name == "erp-etl"


def test_from_env_strips_surrounding_whitespace() -> None:
    """A trailing newline in a .env file must not become part of the host."""
    env = dict(_MINIMAL_ENV) | {"DATABASE_HOST": "  db.internal \n"}
    assert DatabaseSettings.from_env(env).host == "db.internal"


@pytest.mark.parametrize("value", ["", "  ", "not-a-number", "5432.0"])
def test_from_env_rejects_a_non_numeric_port(value: str) -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_PORT": value}
    if value.strip() == "":
        # An empty value falls back to the documented default rather than
        # failing, because an unset variable is a normal local-dev situation.
        assert DatabaseSettings.from_env(env).port == 5432
    else:
        with pytest.raises(ConfigurationError, match="DATABASE_PORT"):
            DatabaseSettings.from_env(env)


@pytest.mark.parametrize("value", ["0", "65536", "-1", "99999"])
def test_from_env_rejects_an_out_of_range_port(value: str) -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_PORT": value}
    with pytest.raises(ConfigurationError):
        DatabaseSettings.from_env(env)


def test_from_env_rejects_a_missing_database_name() -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_NAME": ""}
    with pytest.raises(ConfigurationError, match="DATABASE_NAME"):
        DatabaseSettings.from_env(env)


def test_from_env_rejects_a_missing_user() -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_USER": ""}
    with pytest.raises(ConfigurationError, match="DATABASE_USER"):
        DatabaseSettings.from_env(env)


def test_from_env_rejects_a_negative_statement_timeout() -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_STATEMENT_TIMEOUT_MS": "-1"}
    with pytest.raises(ConfigurationError, match="STATEMENT_TIMEOUT"):
        DatabaseSettings.from_env(env)


def test_settings_are_immutable() -> None:
    """Connection settings must not be mutated after start-up."""
    settings = DatabaseSettings.from_env(dict(_MINIMAL_ENV))
    with pytest.raises(AttributeError):
        settings.password = "other"  # type: ignore[misc]


def test_get_database_settings_delegates_to_from_env() -> None:
    assert get_database_settings(dict(_MINIMAL_ENV)) == DatabaseSettings.from_env(
        dict(_MINIMAL_ENV)
    )


# ---------------------------------------------------------------------------
# 2. DSN construction and secret masking (§10)
# ---------------------------------------------------------------------------


def test_dsn_uses_the_psycopg_driver_by_default() -> None:
    dsn = DatabaseSettings.from_env(dict(_MINIMAL_ENV)).dsn()
    assert dsn.startswith("postgresql+psycopg://")
    assert dsn.endswith("@db.internal:5433/erp_prod")


def test_dsn_percent_encodes_special_characters() -> None:
    """A password containing @ / : must not break the URL."""
    env = dict(_MINIMAL_ENV) | {"DATABASE_PASSWORD": "p@ss:w/rd?#&"}
    dsn = DatabaseSettings.from_env(env).dsn()
    assert "p@ss:w/rd?#&" not in dsn
    assert "p%40ss%3Aw%2Frd%3F%23%26" in dsn
    # The host part must remain parseable: exactly one '@' separator.
    assert dsn.count("@") == 1


def test_dsn_omits_the_colon_when_there_is_no_password() -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_PASSWORD": ""}
    dsn = DatabaseSettings.from_env(env).dsn()
    assert dsn == "postgresql+psycopg://erp_app@db.internal:5433/erp_prod"


def test_safe_dsn_never_reveals_the_password() -> None:
    """§10 — the diagnostic form is the only one that may reach a log."""
    env = dict(_MINIMAL_ENV) | {"DATABASE_PASSWORD": "topsecret"}
    settings = DatabaseSettings.from_env(env)
    safe = settings.safe_dsn()
    assert "topsecret" not in safe
    assert ":***@" in safe
    assert safe == "postgresql+psycopg://erp_app:***@db.internal:5433/erp_prod"


def test_safe_dsn_masks_even_a_password_that_needs_encoding() -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_PASSWORD": "p@ss:w/rd"}
    safe = DatabaseSettings.from_env(env).safe_dsn()
    assert "p%40ss" not in safe
    assert "p@ss" not in safe
    assert ":***@" in safe


def test_safe_dsn_shows_no_mask_when_there_is_no_password() -> None:
    """Masking an absent password would imply a secret that does not exist."""
    env = dict(_MINIMAL_ENV) | {"DATABASE_PASSWORD": ""}
    assert "***" not in DatabaseSettings.from_env(env).safe_dsn()


# ---------------------------------------------------------------------------
# 2a. TD-011 (RESOLVED) — the password cannot leak through any rendering
# ---------------------------------------------------------------------------

_SECRET = "topsecret_value_42"


@pytest.fixture
def secret_settings() -> DatabaseSettings:
    return DatabaseSettings.from_env(dict(_MINIMAL_ENV) | {"DATABASE_PASSWORD": _SECRET})


def test_repr_of_settings_hides_the_password(
    secret_settings: DatabaseSettings,
) -> None:
    """
    TD-011: a bare str field leaked through the generated __repr__.

    A repr reaches a log through logging.exception, a traceback frame, an error
    tracker or pytest output, so this is a security control, not cosmetics.
    """
    rendered = repr(secret_settings)
    assert _SECRET not in rendered
    assert "Secret(***)" in rendered


def test_str_of_settings_hides_the_password(
    secret_settings: DatabaseSettings,
) -> None:
    assert _SECRET not in str(secret_settings)


def test_fstring_of_settings_hides_the_password(
    secret_settings: DatabaseSettings,
) -> None:
    """f-strings use __format__/__str__, which must be covered too."""
    assert _SECRET not in f"{secret_settings}"
    assert _SECRET not in f"{secret_settings!r}"
    assert _SECRET not in f"{secret_settings.password}"
    assert _SECRET not in f"{secret_settings.password!r}"
    assert _SECRET not in f"{secret_settings.password:>40}"
    assert _SECRET not in f"{secret_settings.password}"
    assert _SECRET not in format(secret_settings.password)


def test_logging_the_settings_object_does_not_leak(
    secret_settings: DatabaseSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """The realistic accident: someone logs the settings object directly."""
    import logging

    with caplog.at_level(logging.INFO):
        logging.getLogger("erp.test").info("connecting with %r", secret_settings)
        logging.getLogger("erp.test").info("connecting with %s", secret_settings)
    assert _SECRET not in caplog.text


def test_traceback_of_a_failure_does_not_leak(
    secret_settings: DatabaseSettings,
) -> None:
    """
    A raised exception carrying the settings must not print the secret.

    pytest renders local variables of the failing frame; before TD-011 that
    alone was enough to publish the production password into CI output.
    """
    import traceback

    try:
        raise RuntimeError(f"cannot connect: {secret_settings!r}")
    except RuntimeError as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert _SECRET not in rendered


def test_secret_only_reveals_through_the_explicit_method() -> None:
    """
    reveal() is the single sanctioned reader.

    Keeping it as a named method makes every read greppable in review, which
    is the actual control — the wrapper cannot stop deliberate disclosure.
    """
    secret = Secret("abc")
    assert secret.reveal() == "abc"
    assert repr(secret) == "Secret(***)"
    assert str(secret) == "***"


def test_secret_does_not_compare_equal_to_the_raw_string() -> None:
    """
    Comparison against a plain str must not confirm a guess.

    Otherwise a test (or an attacker with a comparison oracle) could verify a
    candidate password without ever reading it.
    """
    secret = Secret("abc")
    assert secret != "abc"
    assert secret == Secret("abc")


def test_empty_secret_is_falsy_and_renders_empty() -> None:
    """An absent password must be distinguishable from a present one."""
    empty = Secret("")
    assert not empty
    assert str(empty) == ""
    assert repr(empty) == "Secret(empty)"
    assert bool(Secret("x")) is True


def test_secret_length_is_available_without_the_value() -> None:
    """Diagnostics such as "password is 3 characters" must not need reveal()."""
    assert len(Secret("abc")) == 3


def test_settings_dict_dump_does_not_leak(
    secret_settings: DatabaseSettings,
) -> None:
    """
    dataclasses.asdict() is a common serialisation path (config endpoints).

    It must yield the wrapper, not the raw string.
    """
    import dataclasses

    dumped = dataclasses.asdict(secret_settings)
    assert _SECRET not in repr(dumped)
    assert isinstance(dumped["password"], Secret)


# ---------------------------------------------------------------------------
# 3. connect_args — UTC and statement timeout (§15)
# ---------------------------------------------------------------------------


def test_connect_args_force_utc() -> None:
    args = DatabaseSettings.from_env(dict(_MINIMAL_ENV)).connect_args()
    assert "-c timezone=UTC" in args["options"]


def test_connect_args_set_a_statement_timeout() -> None:
    env = dict(_MINIMAL_ENV) | {"DATABASE_STATEMENT_TIMEOUT_MS": "7500"}
    args = DatabaseSettings.from_env(env).connect_args()
    assert "-c statement_timeout=7500" in args["options"]


def test_connect_args_report_the_application_name() -> None:
    """pg_stat_activity must show which component holds a connection."""
    env = dict(_MINIMAL_ENV) | {"DATABASE_APP_NAME": "erp-etl"}
    assert DatabaseSettings.from_env(env).connect_args()["application_name"] == "erp-etl"


# ---------------------------------------------------------------------------
# 4. Live engine behaviour (§15)
# ---------------------------------------------------------------------------


def test_engine_session_runs_in_utc(engine: Engine) -> None:
    """§15 — the connection's TimeZone is UTC, not the server's local zone."""
    with engine.connect() as connection:
        timezone = connection.execute(text("SHOW TimeZone")).scalar_one()
    assert timezone == "UTC"


def test_engine_reports_the_application_name(engine: Engine) -> None:
    with engine.connect() as connection:
        name = connection.execute(text("SELECT current_setting('application_name')")).scalar_one()
    assert name == "erp-backend"


def test_engine_applies_the_statement_timeout(engine: Engine) -> None:
    with engine.connect() as connection:
        value = connection.execute(text("SELECT current_setting('statement_timeout')")).scalar_one()
    assert value == "30s", f"unexpected statement_timeout: {value}"


# ---------------------------------------------------------------------------
# 5. set_current_user — the §54 contract with fn_current_app_user()
# ---------------------------------------------------------------------------


def test_current_app_user_is_null_when_unset(session: Session, applied_schema: None) -> None:
    """
    An unset variable yields NULL, never a fabricated identity (§54).

    fn_current_app_user() must tolerate the variable being absent, because
    migrations and maintenance scripts do not set it.
    """
    value = session.execute(text("SELECT fn_current_app_user()")).scalar_one_or_none()
    assert value is None


def test_set_current_user_is_visible_to_the_database(
    session: Session, applied_schema: None
) -> None:
    set_current_user(session, 12345)
    value = session.execute(text("SELECT fn_current_app_user()")).scalar_one()
    assert value == 12345


def test_set_current_user_none_clears_the_value(session: Session, applied_schema: None) -> None:
    set_current_user(session, 777)
    assert session.execute(text("SELECT fn_current_app_user()")).scalar_one() == 777
    set_current_user(session, None)
    assert session.execute(text("SELECT fn_current_app_user()")).scalar_one_or_none() is None


def test_set_current_user_rejects_a_non_integer(session: Session, applied_schema: None) -> None:
    """
    A non-numeric user id is refused in Python, not written to the session.

    int() is applied deliberately so that a string coming from a request body
    cannot be injected into set_config().
    """
    with pytest.raises((TypeError, ValueError)):
        set_current_user(session, "1 OR TRUE")  # type: ignore[arg-type]


def test_set_current_user_is_transaction_local(engine: Engine, applied_schema: None) -> None:
    """
    The value must not survive the transaction (§54).

    set_config(..., TRUE) is what prevents one request's identity from leaking
    into the next request that reuses the pooled connection. A regression here
    would attribute audit rows to the wrong user.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SELECT set_config('app.current_user_id', '4242', TRUE)"))
        assert connection.execute(text("SELECT fn_current_app_user()")).scalar_one() == 4242
        transaction.rollback()

        # Same physical connection, new transaction: the value is gone.
        transaction = connection.begin()
        assert connection.execute(text("SELECT fn_current_app_user()")).scalar_one_or_none() is None
        transaction.rollback()


def test_audit_trigger_records_the_current_user(session: Session, applied_schema: None) -> None:
    """
    End-to-end §54: an INSERT made after set_current_user() is attributed.

    This is the reason set_current_user() exists; asserting the plumbing
    without asserting the audit row would leave the actual requirement
    unverified. The session fixture rolls everything back.
    """
    actor_login = "audit_probe_user"
    actor_id = session.execute(
        text(
            """
            INSERT INTO users (username, email, password_hash, full_name, is_active)
            VALUES (:username, :email, :password_hash, :full_name, TRUE)
            RETURNING id
            """
        ),
        {
            "username": actor_login,
            "email": "audit_probe@example.invalid",
            # bcrypt-shaped value: the CHECK constraint rejects plaintext (§55).
            "password_hash": "$2b$12$" + "a" * 53,
            "full_name": "Audit Probe",
        },
    ).scalar_one()

    set_current_user(session, actor_id)

    # roles carries an fn_audit() trigger. Note that not every table is
    # audited: STEP 1 attached the trigger to the 19 security- and
    # finance-relevant tables only, so a probe on e.g. categories would
    # (correctly) find no audit row.
    subject_id = session.execute(
        text(
            """
            INSERT INTO roles (code, name, description, is_system)
            VALUES ('AUDIT_PROBE', 'Audit probe', 'temporary test role', FALSE)
            RETURNING id
            """
        )
    ).scalar_one()

    audit_user_id = session.execute(
        text(
            """
            SELECT user_id
              FROM audit_log
             WHERE entity = 'roles'
               AND entity_id = :entity_id
               AND action = 'INSERT'
             ORDER BY id DESC
             LIMIT 1
            """
        ),
        {"entity_id": subject_id},
    ).scalar_one()

    assert audit_user_id == actor_id, (
        "the audit trigger did not pick up app.current_user_id; "
        "audit rows would be written without an acting user (§54)"
    )
