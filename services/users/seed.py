"""Bootstrap a privileged account.

`POST /users` always creates a plain `user`, by design. The first admin (and any
manager) has to be created out of band, which is what this script is for.

    uv run seed-user --email admin@trackflow.com --role admin
"""

import argparse
import getpass
import sys

from pydantic import ValidationError

from services.core.errors import EmailAlreadyRegistered
from services.users import service as users_service
from services.users.models import Role, UserCreate, UserUpdate


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or promote a TrackFlow user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="Prompted for if omitted.")
    parser.add_argument(
        "--role",
        default=Role.ADMIN.value,
        choices=[role.value for role in Role],
    )
    parser.add_argument("--name", default=None, help="Display name for the linked profile.")
    args = parser.parse_args()

    role = Role(args.role)
    password = args.password or getpass.getpass("Password: ")

    existing = users_service.get_user_by_email(args.email)
    if existing is not None:
        if existing.role is role:
            print(f"{existing.email} already exists with role {role.value}. Nothing to do.")
            return
        users_service.update_user(existing.id, UserUpdate(role=role))
        print(f"Promoted {existing.email} from {existing.role.value} to {role.value}.")
        return

    try:
        payload = UserCreate(email=args.email, password=password, name=args.name)
    except ValidationError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error

    try:
        user = users_service.create_user(payload)
    except EmailAlreadyRegistered as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error

    users_service.update_user(user.id, UserUpdate(role=role))
    print(f"Created {user.email} with role {role.value} (id {user.id}).")


if __name__ == "__main__":
    main()
