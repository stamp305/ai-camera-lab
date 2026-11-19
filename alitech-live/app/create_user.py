"""Add a lab portal account:  python -m app.create_user <email> "<name>" [admin|member]

The password is asked for on the terminal (never an argument, so it stays out of the
shell history and `ps`). Only role=admin (Alitech back office) may watch cameras.
"""
import getpass
import sys

from app import users


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    email, name = sys.argv[1], sys.argv[2]
    role = sys.argv[3] if len(sys.argv) > 3 else "admin"
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Again: "):
        sys.exit("passwords differ")
    user = users.create(email, password, name, role)
    print(f"created user id={user['id']} role={user['role']}")


if __name__ == "__main__":
    main()
