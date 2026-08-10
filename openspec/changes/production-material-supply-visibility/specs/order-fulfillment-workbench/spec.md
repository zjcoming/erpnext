## MODIFIED Requirements

### Requirement: Batch Material Shortage And Supply Visibility

The system SHALL calculate raw material coverage for production demand and SHALL expose, per material, the specific outstanding Material Request and Purchase Order documents behind the open-supply totals, including document type, name, status, outstanding quantity, and schedule date. The summed open Material Request and open Purchase Order quantities SHALL be derived from those same documents so the totals are unchanged.

#### Scenario: Expose linked purchase documents per material

- **WHEN** a material has outstanding purchase Material Requests or Purchase Orders in the resolved source warehouse
- **THEN** the coverage result attaches a `supply_documents` list for that material, one entry per outstanding document with its type, name, status, outstanding quantity, and schedule date

#### Scenario: Summary quantities stay consistent with documents

- **WHEN** the open Material Request and open Purchase Order quantities are reported for a material
- **THEN** each equals the sum of the outstanding quantities of the corresponding attached documents

#### Scenario: Show purchase documents and status in the production workbench

- **WHEN** an owner expands a shortage demand in the production workbench
- **THEN** each material row lists its linked Material Request and Purchase Order documents with status, outstanding quantity, and a link to the standard document form, and a material with no purchase documents shows a "not yet ordered" hint

#### Scenario: Purchase consolidation and shortage calculation unchanged

- **WHEN** supply documents are exposed
- **THEN** the system does not change how Material Requests are consolidated for selected shortage rows and does not change how raw material shortages are calculated
