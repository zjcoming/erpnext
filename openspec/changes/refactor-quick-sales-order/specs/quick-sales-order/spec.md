## MODIFIED Requirements

### Requirement: Simplified Sales Order Entry

The system SHALL provide a quick order entry page for routine single-company sales that requires only customer, one order-level delivery date, product, quantity greater than zero, and transaction unit price greater than zero. The page SHALL allow an optional customer purchase order number and one order-level remark, SHALL calculate line and order amounts, and SHALL derive company, currency, selling UOM and conversion factor, finished-goods warehouse, price defaults, and BOM information from ERPNext configuration and master data.

#### Scenario: Create a routine quick order

- **WHEN** a permitted user enters all five required values for one or more distinct products
- **THEN** the system prepares a standard ERPNext Sales Order whose item rows use the order-level delivery date and derived ERPNext defaults

#### Scenario: Reject a zero or negative commercial value

- **WHEN** any entered quantity or transaction unit price is zero, negative, missing, or not a valid number
- **THEN** the system blocks confirmation and identifies the affected row in Chinese

#### Scenario: Reject an invalid delivery date

- **WHEN** the order-level delivery date is missing or earlier than the current business date
- **THEN** the system blocks confirmation with a Chinese explanation

#### Scenario: Reject duplicate finished-good rows

- **WHEN** the same product appears more than once in the quick order
- **THEN** the system blocks confirmation and tells the user to merge the rows or use the standard Sales Order for different commercial terms

#### Scenario: Preserve optional commercial context

- **WHEN** the user provides a customer purchase order number or order remark
- **THEN** the values are stored in the corresponding standard Sales Order fields without encoding quick-order control flags into the terms text

#### Scenario: Derive defaults without exposing advanced fields

- **WHEN** the page loads or a product is selected
- **THEN** company, currency, UOM, conversion factor, finished-goods warehouse, price defaults, and BOM information are resolved from ERPNext and are not editable on the quick page

### Requirement: Confirmed Quick Orders Submit Standard Sales Orders

The system SHALL perform an authoritative server-side preflight before showing final confirmation and SHALL repeat all mutable validations immediately before creating and submitting a standard ERPNext Sales Order. The final confirmation SHALL summarize the order total, finished-goods coverage, production demand, material-shortage count, blocking issues, and warnings reviewed by the user.

#### Scenario: Submit an unchanged reviewed order

- **WHEN** a user confirms a preflight result and the repeated validation has the same material outcome
- **THEN** the system creates and submits one standard ERPNext Sales Order and routes the user to that order's fulfillment workbench

#### Scenario: Require reconfirmation after material change

- **WHEN** reservable stock, production demand, BOM readiness, shortage state, price, customer status, credit result, or another material validation outcome changes after the reviewed preflight
- **THEN** the system does not create the order, refreshes the confirmation summary, and requires the user to confirm the new result

#### Scenario: Preserve ERPNext validation

- **WHEN** ERPNext rejects the Sales Order because of permissions, master data, pricing, warehouse, credit, fiscal, or document validation
- **THEN** the system creates no submitted order and shows a Chinese-facing error that preserves the actionable ERPNext reason

#### Scenario: Prevent duplicate submission after retry

- **WHEN** the client repeats a submit request with the same idempotency key because of double-click, timeout, or network retry
- **THEN** the system returns the same Sales Order result and does not create a second order

## ADDED Requirements

### Requirement: Minimal Quick-Order Surface

The system SHALL omit partial-delivery selection, warehouse selection, per-line delivery date, line remarks, and advanced accounting or logistics fields from the quick page. The page SHALL provide a clear route to the standard Sales Order for cases the quick flow does not support.

#### Scenario: Routine page load

- **WHEN** the owner opens the quick-order page
- **THEN** only customer, delivery date, optional customer PO number, optional order remark, product, quantity, transaction unit price, calculated amount, and fulfillment information are presented

#### Scenario: Need different delivery dates

- **WHEN** a user needs different delivery dates for different products
- **THEN** the quick page directs the user to use the standard Sales Order instead of adding per-line dates

#### Scenario: Need an all-at-once delivery promise

- **WHEN** a user needs to record or enforce an all-at-once delivery commitment
- **THEN** the quick page directs the user to the standard Sales Order or records the commitment as an ordinary remark, while ERPNext retains its standard partial-delivery behavior

### Requirement: Layered Fulfillment Checking

The system SHALL distinguish a lightweight automatic availability preview from an explicit deep material check. The lightweight preview SHALL use ERPNext's reservable finished-goods quantity rather than raw warehouse balance, and the deep check SHALL explode the selected BOMs and evaluate material demand using the shared shortage-calculation rules.

#### Scenario: Product or quantity stabilizes

- **WHEN** a valid product is selected or its quantity stops changing for 500 to 800 milliseconds
- **THEN** the page requests item defaults, transaction price, reservable finished-goods quantity, production demand, and active default BOM readiness without running a full BOM explosion

#### Scenario: User requests a deep check

- **WHEN** the user selects “检查库存与缺料”
- **THEN** the server calculates fulfillment and raw-material shortage for the complete current order and returns a timestamped result

#### Scenario: Demand changes after a check

- **WHEN** customer, delivery date, product, quantity, or price changes after a check result is shown
- **THEN** the page labels the result “待重新检查” and does not present it as current

#### Scenario: Stock balance includes unavailable quantity

- **WHEN** physical stock exists but some quantity is already reserved or otherwise unavailable to this order
- **THEN** the preview reports only the quantity ERPNext says is available to reserve as immediate finished-goods coverage

### Requirement: Compact Fulfillment Visibility

The system SHALL show, per product, the requested quantity, reservable finished-goods coverage, required production quantity, BOM readiness, and material-shortage state, and SHALL show an order-level summary with total amount, coverage, production demand, shortage item count, and last-check time.

#### Scenario: Finished stock fully covers demand

- **WHEN** reservable finished-goods quantity covers the requested quantity
- **THEN** the row is shown as stock-covered and absence of a BOM does not block the order

#### Scenario: Production is required and BOM is ready

- **WHEN** reservable finished goods do not cover demand and an active submitted default BOM is available
- **THEN** the row shows the uncovered quantity as production demand and the BOM as ready

#### Scenario: Production is required without a usable BOM

- **WHEN** reservable finished goods do not cover demand and no active submitted default BOM can be resolved
- **THEN** the row shows a blocking BOM issue and final confirmation is unavailable

#### Scenario: Material shortage exists

- **WHEN** the deep check finds insufficient raw material for planned production
- **THEN** the page shows the shortage item count and affected rows as warnings without preventing order creation solely because of shortage

### Requirement: Authoritative Preflight Classification

The preflight SHALL classify issues as blockers or warnings. It MUST block unavailable permissions, missing or disabled customer/product, non-sales products, invalid quantity/rate/date/UOM, duplicate product rows, undeterminable company or finished-goods warehouse, production demand without a usable BOM, duplicate customer PO when ERPNext disallows it, failed credit checks, and any standard ERPNext validation. It SHALL treat finished-goods insufficiency and raw-material shortage as warnings.

#### Scenario: Warning-only order

- **WHEN** an order has finished-goods or raw-material shortage but all required master data, BOMs, commercial data, permissions, and ERPNext validations are valid
- **THEN** the confirmation clearly shows the warnings and allows the user to continue

#### Scenario: Blocking master-data issue

- **WHEN** the customer or product is missing, disabled, invalid for sales, or not readable by the user
- **THEN** confirmation is blocked without exposing unauthorized record details

#### Scenario: Duplicate customer purchase order

- **WHEN** the same customer PO number already belongs to another non-cancelled Sales Order and ERPNext settings disallow multiple Sales Orders against it
- **THEN** the quick flow blocks submission using the standard ERPNext rule

### Requirement: BOM Snapshot for Order-Driven Production

The system SHALL snapshot the resolved active default BOM into the standard Sales Order Item BOM field whenever production is required, and downstream order-driven production SHALL prefer that order-line BOM over a later default BOM lookup.

#### Scenario: Default BOM changes after ordering

- **WHEN** a product's default BOM changes after the quick Sales Order was submitted
- **THEN** a Work Order created for the Sales Order Item uses the BOM recorded on that order line

#### Scenario: Stock-covered product has no BOM

- **WHEN** the order is fully covered by reservable finished goods and no BOM exists
- **THEN** the order may be submitted with no BOM snapshot for that row

### Requirement: Standard ERPNext Records Remain Authoritative

The quick-order flow MUST NOT create a custom order, inventory, reservation, production, purchase, or delivery ledger and MUST NOT automatically reserve stock, create Work Orders, create Material Requests, or post stock movements during order submission.

#### Scenario: Inspect a submitted quick order

- **WHEN** a quick order succeeds
- **THEN** authoritative commercial data exists only in standard Sales Order and Sales Order Item records

#### Scenario: Continue fulfillment

- **WHEN** the user proceeds from the submitted order to reservation, production, shortage purchasing, or delivery actions
- **THEN** each later action uses its existing guided workflow and repeats its own current-state validations

### Requirement: Complex Orders Use the Standard Sales Order

The quick flow SHALL reject or redirect cases requiring multiple delivery dates, duplicate lines for different prices, advanced UOM or currency handling, complex tax overrides, product bundles or gifts, subcontracting, customer-supplied materials, serial/batch selection, or multiple companies.

#### Scenario: Unsupported quick-order payload

- **WHEN** a client submits advanced fields or a case outside the supported quick-order contract
- **THEN** the server ignores no safety-critical information, creates no order, and returns a Chinese explanation directing the user to standard Sales Order entry
