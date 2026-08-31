from process_simplification.management_access import (
	ensure_management_access,
	migrate_management_users_to_role_profiles,
	retire_legacy_management_roles,
)


def execute():
	ensure_management_access()
	migrate_management_users_to_role_profiles()
	retire_legacy_management_roles()
