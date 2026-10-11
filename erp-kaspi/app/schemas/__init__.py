"""Pydantic v2 schemas — the API contract layer (MASTER PROMPT §12).

Split into domain modules; this package re-exports everything so
``from app.schemas import X`` keeps working (R1 mechanical split).
"""

from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    TokenResponse,
    UserCreate,
)
from app.schemas.bank import (
    BankLineConfirmRequest,
    BankLineIgnoreRequest,
    BankLinesPostRequest,
    BankLinesSuggestRequest,
    PostingPolicySchema,
)
from app.schemas.common import (
    EmployeePaymentType,
    Money,
    OperationDirection,
    OrderStatus,
    PaymentMethod,
    Quantity,
)
from app.schemas.finance import (
    OperationCreate,
    OperationRead,
    TransferCreate,
    TransferRead,
)
from app.schemas.inventory import (
    ProductionOutputCreate,
    ReservationCreate,
    ReservationReleaseCreate,
    StockAdjustmentCreate,
    StockIssueCreate,
    StockReceiptCreate,
    StockScrapCreate,
)
from app.schemas.kaspi import (
    KaspiPayload,
    KaspiPayoutCreate,
    ReconciliationRule,
)
from app.schemas.orders import (
    OrderCreate,
    OrderItemCreate,
    OrderMaterialCreate,
    OrderRead,
    OrderStatusChange,
)
from app.schemas.payroll import (
    PayrollAdjustment,
    PayrollCalculateRequest,
    PayrollPayRequest,
    PayrollPeriodCreate,
    WorkEntryCreate,
    WorkEntryRead,
)

__all__ = [
    "BankLineConfirmRequest",
    "BankLineIgnoreRequest",
    "BankLinesPostRequest",
    "BankLinesSuggestRequest",
    "EmployeePaymentType",
    "KaspiPayload",
    "KaspiPayoutCreate",
    "LoginRequest",
    "Money",
    "OperationCreate",
    "OperationDirection",
    "OperationRead",
    "OrderCreate",
    "OrderItemCreate",
    "OrderMaterialCreate",
    "OrderRead",
    "OrderStatus",
    "OrderStatusChange",
    "PasswordChangeRequest",
    "PasswordResetRequest",
    "PaymentMethod",
    "PayrollAdjustment",
    "PayrollCalculateRequest",
    "PayrollPayRequest",
    "PayrollPeriodCreate",
    "PostingPolicySchema",
    "ProductionOutputCreate",
    "Quantity",
    "ReconciliationRule",
    "ReservationCreate",
    "ReservationReleaseCreate",
    "StockAdjustmentCreate",
    "StockIssueCreate",
    "StockReceiptCreate",
    "StockScrapCreate",
    "TokenResponse",
    "TransferCreate",
    "TransferRead",
    "UserCreate",
    "WorkEntryCreate",
    "WorkEntryRead",
]
