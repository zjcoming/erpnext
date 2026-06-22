## ADDED Requirements

### Requirement: Simplified Sales Order Entry

The system SHALL provide a simplified quick order entry flow for small factory users that captures only customer, product, quantity, unit price, delivery date, partial-delivery allowance, and remarks for each order line.

#### Scenario: Create quick Sales Order

- **WHEN** a user confirms a valid quick order
- **THEN** the system creates an ERPNext Sales Order with Sales Order Item rows matching the entered products, quantities, prices, delivery dates, warehouses, and remarks

#### Scenario: Reject duplicate finished good rows

- **WHEN** a user enters the same finished good more than once in a single quick order
- **THEN** the system blocks submission with a Chinese message explaining that phase one requires one row per finished good and the user must split the demand or merge the row

### Requirement: Confirmed Quick Orders Submit Standard Sales Orders

The system SHALL require explicit confirmation before submitting a quick Sales Order and SHALL submit the standard ERPNext Sales Order after confirmation.

#### Scenario: Submit after confirmation

- **WHEN** a user confirms submission of a newly created quick order
- **THEN** the standard ERPNext Sales Order is submitted and the user is taken to the order workbench for that order

#### Scenario: Preserve ERPNext validation

- **WHEN** ERPNext rejects the Sales Order because required master data, pricing, warehouse, credit, or permission validation fails
- **THEN** the system does not create a submitted order and shows a Chinese-facing error that preserves the ERPNext validation reason

### Requirement: No Custom Order Ledger

The system MUST NOT create or maintain a custom order DocType as the source of truth for quick orders.

#### Scenario: Inspect created order

- **WHEN** a quick order is created
- **THEN** all authoritative order data exists in standard ERPNext Sales Order and Sales Order Item records
