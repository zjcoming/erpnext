## ADDED Requirements

### Requirement: Sales Order Item Workbench Rows

The system SHALL show order workbench rows at Sales Order Item grain and SHALL use Sales Order Item identity as the primary key for reservation, production, completion, shortage, and delivery calculations.

#### Scenario: Load submitted Sales Order

- **WHEN** a user opens the workbench for a submitted Sales Order
- **THEN** the system displays one row per supported Sales Order Item with order quantity, delivered quantity, pending delivery quantity, effective reservation, active production coverage, completed quantity, uncovered quantity, material status, current status, and next actions

#### Scenario: Unsupported duplicate item rows

- **WHEN** a standard ERPNext Sales Order contains duplicate supported finished-good rows that cannot be safely disambiguated
- **THEN** the system flags those rows as unsupported for simplified actions and does not create reservations, Work Orders, or Delivery Notes from ambiguous row aggregation

### Requirement: Effective Finished Goods Reservation

The system SHALL calculate effective finished-goods reservation from submitted Stock Reservation Entries linked to the current Sales Order Item and SHALL count only the remaining reserved quantity.

#### Scenario: Count active reservation

- **WHEN** a Sales Order Item has submitted Stock Reservation Entries with remaining reserved quantity
- **THEN** the workbench shows `reserved_qty - delivered_qty - transferred_qty - consumed_qty` as effective reservation for that item

#### Scenario: Ignore cancelled or delivered reservation

- **WHEN** a Stock Reservation Entry is cancelled or has no remaining reserved quantity
- **THEN** the workbench does not count that entry as effective reservation

### Requirement: Reserve Available Finished Goods

The system SHALL allow users to reserve currently available finished goods for a Sales Order Item by creating and submitting standard Stock Reservation Entries.

#### Scenario: Partially reserve available stock

- **WHEN** an order line has pending delivery quantity and only part of that quantity is available after other reservations
- **THEN** the system creates and submits a Stock Reservation Entry for no more than the available unreserved quantity and no more than the pending delivery quantity

#### Scenario: Prevent over-reservation

- **WHEN** a user attempts to reserve more than the current unreserved warehouse stock or more than the Sales Order Item pending delivery quantity
- **THEN** the system blocks the action with a Chinese validation message before submitting the Stock Reservation Entry

### Requirement: Active Production Coverage

The system SHALL calculate active production coverage from submitted Work Orders linked to the current Sales Order Item and SHALL exclude cancelled, closed, stopped, and completed coverage.

#### Scenario: Calculate uncovered quantity

- **WHEN** a Sales Order Item has pending delivery quantity, effective reservation, and active Work Order coverage
- **THEN** the system calculates uncovered quantity as `max(0, pending delivery quantity - effective reservation - active production coverage)`

#### Scenario: Exclude completed Work Order from active coverage

- **WHEN** a linked Work Order is completed and its produced quantity is already represented by Manufacture Stock Entry output
- **THEN** the system does not count that completed quantity as active production coverage

### Requirement: Create Sales Order Linked Work Orders

The system SHALL allow users to create and submit standard Work Orders only for uncovered quantity and MUST link each Work Order to the Sales Order and Sales Order Item.

#### Scenario: Create Work Order for uncovered demand

- **WHEN** uncovered quantity is greater than zero and the item has a valid BOM and required warehouses
- **THEN** the system creates and submits a Work Order with `sales_order`, `sales_order_item`, production item, BOM, quantity, source warehouse, WIP warehouse, and finished goods warehouse populated

#### Scenario: Prevent duplicate or excess production

- **WHEN** existing active linked Work Orders already cover the Sales Order Item pending demand after effective reservation
- **THEN** the system blocks creation of another Work Order and shows the user a Chinese message explaining that production is already covered

### Requirement: Completed Production Reservation Check

The system SHALL detect finished goods completed from Sales Order linked Work Orders that are not effectively reserved for the target Sales Order Item and SHALL provide a guided reservation action.

#### Scenario: Completed stock is already reserved

- **WHEN** Manufacture Stock Entry output from a linked Work Order has resulted in effective Sales Order Item reservation
- **THEN** the workbench includes the completed stock in effective reservation and can show the row as available for delivery

#### Scenario: Completed stock is not reserved

- **WHEN** linked Work Order finished goods have been manufactured into the finished goods warehouse but are not effectively reserved for the Sales Order Item
- **THEN** the workbench shows "完工待预留" and offers "预留完工成品" instead of treating the stock as freely available to other simplified orders

### Requirement: Create Reserved Stock Delivery Note Drafts

The system SHALL create standard Delivery Note drafts from Sales Orders for effective reserved stock and MUST NOT auto-submit Delivery Notes.

#### Scenario: Create Delivery Note draft for reserved quantity

- **WHEN** a Sales Order Item has effective reservation and the user confirms delivery creation
- **THEN** the system creates a standard Delivery Note draft whose item quantity does not exceed the effective reserved quantity and whose rows reference the Sales Order and Sales Order Item

#### Scenario: Warehouse staff submits physical delivery

- **WHEN** a Delivery Note draft is created from the simplified workbench
- **THEN** the document remains unsubmitted until warehouse staff verifies the physical goods and submits it through ERPNext

### Requirement: Recalculate Status From Standard Documents

The system SHALL recalculate workbench status from ERPNext standard documents on page load and after each simplified action.

#### Scenario: External cancellation reflected

- **WHEN** a user cancels a Stock Reservation Entry, Work Order, Stock Entry, or Delivery Note from a standard ERPNext page
- **THEN** the next workbench load recalculates the row quantities and next action without relying on stale simplified-app state

### Requirement: Read-Only Order Fulfillment Overview

The system SHALL provide an `订单履约总览` that discovers every readable, submitted Sales Order that is neither closed,
completed, nor fully delivered, and that has pending delivery quantity. It SHALL recalculate each order from the
Sales-Order-Item workbench read model without creating or changing ERPNext documents. The overview SHALL include
direct-stock orders and orders whose items have mixed fulfilment states.

#### Scenario: Discover all unfinished orders

- **WHEN** a user with Sales Order read permission opens the overview
- **THEN** the system lists every readable submitted Sales Order with pending delivery quantity, including an order that
  can ship entirely from effective reserved stock and an order that needs production

#### Scenario: Show mixed item states

- **WHEN** one Sales Order has items in more than one state, such as directly deliverable stock, active production, and
  uncovered demand
- **THEN** the overview aggregates the order-level quantities and risk while its expansion exposes the current
  item-level states and next actions from the workbench

#### Scenario: Sort by delivery date and risk

- **WHEN** the overview returns unfinished orders with different delivery dates and risk levels
- **THEN** it orders dated orders by earliest outstanding delivery date first, breaks equal dates by higher risk first,
  and places orders without a delivery date after dated orders

#### Scenario: Focus an order through the route

- **WHEN** a user opens the order-workbench route with a Sales Order identifier
- **THEN** the overview focuses and expands that order when it is visible, while retaining the default overview ordering

#### Scenario: Filter and export the visible read model

- **WHEN** a user applies delivery-window, fulfilment-status, or risk filters, or exports the overview
- **THEN** counters and CSV output reflect only the currently visible orders, and narrow desktop or tablet screens retain
  access to the table through horizontal scrolling

#### Scenario: Respect permissions and recalculate after actions

- **WHEN** a user lacks Sales Order read permission
- **THEN** the system denies overview discovery

- **WHEN** an authorized user completes a simplified row action or returns after a standard ERPNext document change
- **THEN** the overview reloads the standard-document read model and shows the recalculated quantities, state, and risk

### Requirement: Keep Overview Scope Separate From Production Scheduling And Procurement Automation

The system SHALL treat the overview as sales-to-delivery risk visibility and SHALL keep a future production workbench
limited to production execution. It MUST NOT implement production scheduling or automatic procurement through either
surface.

#### Scenario: Inspect overview side effects

- **WHEN** a user loads, refreshes, filters, expands, or exports the overview
- **THEN** the system performs read-only operations and does not reserve stock, create Work Orders, create purchasing
  documents, or submit delivery documents
