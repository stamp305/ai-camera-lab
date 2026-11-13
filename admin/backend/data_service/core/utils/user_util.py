import string
import secrets
from typing import List, Dict

from data_service.core.database.crud.group_crud import read_all_groups
from data_service.core.database.crud.package_crud import read_all_packages
from data_service.core.schemas.user_schemas import GroupedUsers, UserReadResponse

def group_users_by_group(users: List[Dict]) -> List[GroupedUsers]:
    """
    Groups users by their group.

    Args:
        users (list): List of user dictionaries.

    Returns:
        dict: A dictionary where keys are group names and values are lists of users belonging to that group.
    """
    groups = read_all_groups()
    group_map = {group['id']: group['name'] for group in groups}

    package = read_all_packages()
    package_map = {pkg['id']: pkg['name'] for pkg in package}

    grouped: Dict[str, List[UserReadResponse]] = {}

    for user in users:
        group_id = user.get('group_id')
        group_name = group_map.get(group_id)

        if group_name is None:
            group_name = 'Unknown group'

        package_id = user.get('package_id')
        package_name = package_map.get(package_id, 'N/A')

        try:
            user_model = UserReadResponse(
                activation_code = user.get('activation_code'),
                serial_number = user.get('serial_number'),
                mac_address = user.get('mac_address'),
                package = package_name,
                version = user.get('version') or 'N/A',
                is_online = user.get('is_online', 0),
                is_activated = user.get('is_activated', 0),
                start_date = str(user.get('start_date') or 'N/A'),
                last_update = str(user.get('last_update') or 'N/A'),
                branch_name = user.get('branch_name'),
            )
        except Exception as e:
            print(f"Skipping user due to error: {e}")
            continue
            

        grouped.setdefault(group_name.capitalize(), []).append(user_model)
    grouped_users: List[GroupedUsers] = [
        GroupedUsers(group_name=name, users=users_list)
        for name, users_list in sorted(grouped.items(), key=lambda x: x[0].lower())
    ]

    return grouped_users

ALPHABET = string.ascii_uppercase + string.digits

def _generate_random_block(length=4) -> str:
    return ''.join(secrets.choice(ALPHABET) for _ in range(length))

def generate_activation_code() -> str:
    return f"ADV-{_generate_random_block()}-{_generate_random_block()}-{_generate_random_block()}"
