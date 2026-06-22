## ADDED Requirements

### Requirement: Batch Shortage Check For Selected Order Lines

The system SHALL allow users to select one or more workbench order lines and calculate raw material shortage for the selected uncovered production demand.

#### Scenario: Calculate material demand

- **WHEN** a user runs shortage checking for selected order lines with uncovered production demand
- **THEN** the system expands each selected item's BOM, aggregates raw material demand, deducts current available raw material stock, deducts unfinished Purchase Material Requests, deducts unfinished Purchase Orders, and displays the resulting purchase shortage

#### Scenario: No production demand selected

- **WHEN** a user runs shortage checking and no selected line has uncovered production demand or active production material demand
- **THEN** the system shows a Chinese message explaining that there is no material shortage to purchase

### Requirement: Shortage Results Are Adjustable

The system SHALL display shortage results by raw material and SHALL allow users to choose which materials and quantities to include in the purchase request.

#### Scenario: Adjust purchase quantity

- **WHEN** a user changes the proposed purchase quantity for a shortage row
- **THEN** the system uses the adjusted quantity for Material Request creation as long as the quantity is positive and not greater than the calculated shortage unless explicitly allowed by a privileged user

### Requirement: Create Purchase Material Request

The system SHALL create and submit a standard ERPNext Material Request with material request type Purchase from selected shortage rows and MUST NOT create Purchase Orders directly.

#### Scenario: Generate submitted Purchase Material Request

- **WHEN** a user confirms selected shortage rows for purchasing
- **THEN** the system creates and submits a standard Material Request of type Purchase containing the selected raw materials, quantities, schedule dates, warehouses, and source context

#### Scenario: Preserve purchasing workflow

- **WHEN** a Purchase Material Request is created from shortage checking
- **THEN** supplier selection, pricing, Purchase Order creation, and Purchase Receipt remain in the standard ERPNext purchasing flow

### Requirement: Preserve Shortage Explanation

The system SHALL preserve enough source context for users to understand which selected order lines contributed to each aggregated shortage row.

#### Scenario: Show source order explanation

- **WHEN** shortage results aggregate demand from multiple Sales Order Items into one raw material row
- **THEN** the system displays the contributing Sales Orders, Sales Order Items, finished goods, and quantities in the simplified shortage result

#### Scenario: Material Request includes context

- **WHEN** a Material Request is created from aggregated shortage rows
- **THEN** each Material Request Item includes source context in fields available for notes or descriptions without creating a custom purchasing ledger
