## MODIFIED Requirements

### Requirement: Create Sales Order Linked Work Orders

The system SHALL create Sales Order linked Work Orders for a Sales Order Item's uncovered production demand and SHALL, for multi-level BOMs, also create a Work Order for each remaining in-house sub-assembly level in the same action. Work Order creation SHALL reuse ERPNext's Production Plan sub-assembly engine and SHALL use the delivery-priority allocated net quantity as the finished-good planned quantity.

#### Scenario: Create finished-good and sub-assembly Work Orders

- **WHEN** a user creates production for a Sales Order Item whose finished good has a multi-level BOM containing self-made sub-assemblies
- **THEN** the system creates one Work Order for the finished good and one Work Order for each remaining in-house sub-assembly level, and every created Work Order links back to the Sales Order and Sales Order Item

#### Scenario: Net quantity from delivery-priority allocation

- **WHEN** current finished stock has been allocated across open orders in delivery-date order
- **THEN** the finished-good Work Order planned quantity equals the Sales Order Item's remaining unplanned production quantity after that allocation, and the action is rejected if that quantity is not positive

#### Scenario: Skip sub-assembly levels already in stock

- **WHEN** a sub-assembly level is already covered by sub-assembly stock in the resolved production source warehouse
- **THEN** the system does not create a Work Order for that level

#### Scenario: Prefer the BOM snapshotted on the Sales Order Item

- **WHEN** the Sales Order Item carries a snapshotted BOM
- **THEN** the system uses that BOM for the finished-good Work Order instead of resolving the current default BOM

#### Scenario: Traceable creation without leaking engine messages

- **WHEN** the Production Plan engine creates the Work Orders
- **THEN** the created Work Orders carry a Production Plan back-reference, and the engine's native English messages are suppressed while the workbench shows a Chinese summary of how many Work Orders were created, including how many are sub-assemblies
