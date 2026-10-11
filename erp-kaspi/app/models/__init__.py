"""
SQLAlchemy 2.x models — a 1:1 mapping of database/schema.sql.

MASTER PROMPT §80–§82: the 17 original tables are preserved unchanged and the
14 architectural extensions are marked as such in their docstrings.

ORIGINAL TABLES (§80)                 MODEL
  1  ip_entities                      reference.IpEntity
  2  kaspi_api_credentials            kaspi.KaspiApiCredential
  3  operation_types                  reference.OperationType
  4  categories                       reference.Category
  5  counterparties                   reference.Counterparty
  6  orders                           orders.Order
  7  raw_kaspi_payloads               kaspi.RawKaspiPayload
  8  operations                       finance.Operation
  9  transfers                        finance.Transfer
 10  sync_log                         kaspi.SyncLog
 11  employees                        hr.Employee
 12  work_operation_types             hr.WorkOperationType
 13  rates_history                    hr.RateHistory
 14  work_entries                     hr.WorkEntry
 15  payroll_periods                  hr.PayrollPeriod
 16  payroll_summary                  hr.PayrollSummary
 17  payroll_payments                 hr.PayrollPayment

ARCHITECTURAL EXTENSIONS (§82)        MODEL                       REQUIREMENT
  products                            reference.Product           §46
  order_items                         orders.OrderItem            §47
  materials                           reference.Material          §48
  order_materials                     orders.OrderMaterial        §49
  order_status_history                orders.OrderStatusHistory   §50
  accounts                            reference.Account           §51
  users                               auth.User                   §52, §55
  roles                               auth.Role                   §52, §53
  user_roles                          auth.UserRole               §52
  audit_log                           auth.AuditLog               §54
  kaspi_payouts                       kaspi.KaspiPayout           §24, §44
  kaspi_payout_items                  kaspi.KaspiPayoutItem       §44
  tax_rates_history                   reference.TaxRateHistory    §75
  import_batches                      finance.ImportBatch         §39–§41
  raw_bank_statements                 bank.RawBankStatement       G-BANK-2
  bank_statement_lines                bank.BankStatementLine      G-BANK-3
  bank_fee_knp_codes                  bank.BankFeeKnpCode         G-BANK-4

The database remains the source of truth (ADR-011): schema.sql is changed
first, then the models are aligned and verified by
tests/test_models_match_schema.py.
"""

from __future__ import annotations

from app.core.auth import AUDIT_ACTIONS, SYSTEM_ROLE_CODES, AuditLog, Role, User, UserRole
from app.core.base import Base, metadata_obj
from app.models.bank import BankFeeKnpCode, BankStatementLine, RawBankStatement
from app.models.finance import ImportBatch, Operation, Transfer
from app.models.hr import (
    Employee,
    PayrollPayment,
    PayrollPeriod,
    PayrollSummary,
    RateHistory,
    WorkEntry,
    WorkOperationType,
)
from app.models.inventory import (
    STOCK_MOVEMENT_TYPES,
    MaterialReservation,
    ProductionOutput,
    StockMovement,
)
from app.models.kaspi import (
    KaspiApiCredential,
    KaspiPayout,
    KaspiPayoutItem,
    RawKaspiPayload,
    SyncLog,
)
from app.models.orders import (
    ORDER_STATUSES,
    Order,
    OrderItem,
    OrderMaterial,
    OrderStatusHistory,
)
from app.models.reference import (
    Account,
    Category,
    Counterparty,
    IpEntity,
    Material,
    OperationType,
    Product,
    TaxRateHistory,
)

#: The 17 original table names of §80, in the order given by the MASTER PROMPT.
ORIGINAL_TABLE_NAMES: tuple[str, ...] = (
    "ip_entities",
    "kaspi_api_credentials",
    "operation_types",
    "categories",
    "counterparties",
    "orders",
    "raw_kaspi_payloads",
    "operations",
    "transfers",
    "sync_log",
    "employees",
    "work_operation_types",
    "rates_history",
    "work_entries",
    "payroll_periods",
    "payroll_summary",
    "payroll_payments",
)

#: Architectural extension tables (§82) mapped to the requirement they implement.
EXTENSION_TABLE_REQUIREMENTS: dict[str, str] = {
    "products": "§46",
    "order_items": "§47",
    "materials": "§48",
    "order_materials": "§49",
    "order_status_history": "§50",
    "accounts": "§51",
    "users": "§52, §55",
    "roles": "§52, §53",
    "user_roles": "§52",
    "audit_log": "§54",
    "kaspi_payouts": "§24, §44",
    "kaspi_payout_items": "§44",
    "tax_rates_history": "§75",
    "import_batches": "§39–§41",
    "raw_bank_statements": "G-BANK-2",
    "bank_statement_lines": "G-BANK-3",
    "bank_fee_knp_codes": "G-BANK-4",
    "stock_movements": "P2-INV-001",
    "material_reservations": "P2-INV-003",
    "production_output": "G-C2",
}

#: Database views defined in SECTION 8 of schema.sql (§63).
#: Views are intentionally NOT mapped to models — they are read through
#: explicit SQL/Core selects in the reporting layer.
VIEW_NAMES: tuple[str, ...] = (
    "v_ip_financial_summary",
    "v_payroll_due",
    "v_work_entries_detail",
    "v_order_costing",
    "v_kaspi_reconciliation",
    "v_material_stock",
    "v_material_availability",
    "v_wip_by_order",
    "v_finished_goods_stock",
)

__all__ = [
    "AUDIT_ACTIONS",
    "EXTENSION_TABLE_REQUIREMENTS",
    "ORDER_STATUSES",
    "ORIGINAL_TABLE_NAMES",
    "STOCK_MOVEMENT_TYPES",
    "SYSTEM_ROLE_CODES",
    "VIEW_NAMES",
    "Account",
    "AuditLog",
    "BankFeeKnpCode",
    "BankStatementLine",
    "Base",
    "Category",
    "Counterparty",
    "Employee",
    "ImportBatch",
    "IpEntity",
    "KaspiApiCredential",
    "KaspiPayout",
    "KaspiPayoutItem",
    "Material",
    "MaterialReservation",
    "Operation",
    "OperationType",
    "Order",
    "OrderItem",
    "OrderMaterial",
    "OrderStatusHistory",
    "PayrollPayment",
    "PayrollPeriod",
    "PayrollSummary",
    "Product",
    "ProductionOutput",
    "RateHistory",
    "RawBankStatement",
    "RawKaspiPayload",
    "Role",
    "StockMovement",
    "SyncLog",
    "TaxRateHistory",
    "Transfer",
    "User",
    "UserRole",
    "WorkEntry",
    "WorkOperationType",
    "metadata_obj",
]
