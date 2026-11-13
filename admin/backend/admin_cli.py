# backend/admin_cli.py
"""Admin command-line interface."""
from __future__ import annotations

from config import DLC_MASTER_PATH, clients, clients_lock
from dlc_manager import list_dlc_folders
from api_handler import api_broadcast_updates, api_update_specific_client


def run_admin_menu(server_socket) -> None:
    """Run the interactive admin menu."""
    while True:
        print("\n--- Admin Menu ---")
        print("1. List connected clients (Summary)")
        print("2. View client details")
        print("3. PUSH updates to ALL clients")
        print("4. PUSH updates to a SINGLE client")
        print("5. List DLC folders")
        print("q. Quit")
        choice = input("Enter your choice: ").strip()

        if choice == '1':
            with clients_lock:
                if not clients:
                    print("No clients connected.")
                else:
                    print(f"Connected clients ({len(clients)}):")
                    print(f"  {'ID':<38} {'Version':<10} {'Status':<10} {'Package'}")
                    print(f"  {'-'*38} {'-'*10} {'-'*10} {'-'*15}")
                    for client_id, client_data in clients.items():
                        info = client_data.get('info', {})
                        ver = info.get('version', 'N/A')
                        stat = info.get('status', 'N/A')
                        pkg = info.get('package', 'N/A')
                        print(f"  {client_id:<38} {ver:<10} {stat:<10} {pkg}")
        
        elif choice == '2':
            with clients_lock:
                if not clients:
                    print("No clients connected.")
                else:
                    target_id = input("Enter the full ID of the client to view: ").strip()
                    if target_id in clients:
                        print(f"\n--- Details for client: {target_id} ---")
                        info = clients[target_id].get('info', {})
                        for key, value in info.items():
                            print(f"  {key:<15}: {value}")
                        print("------------------------------------------")
                    else:
                        print("Client ID not found.")

        elif choice == '3':
            api_broadcast_updates()
            
        elif choice == '4':
            with clients_lock:
                if not clients:
                    print("No clients connected to update.")
                    target_id = None
                else:
                    target_id = input("Enter the full ID of the client to update: ").strip()
            if target_id:
                api_update_specific_client(target_id)
                
        elif choice == '5':
            print(f"\n--- Folders in '{DLC_MASTER_PATH}' ---")
            folders = list_dlc_folders(DLC_MASTER_PATH)
            if not folders:
                print("No subdirectories found.")
            else:
                print("Available folders for targeted updates:")
                for folder_name in sorted(folders):
                    print(f"  - {folder_name}")
            print("---------------------------------")
            
        elif choice.lower() == 'q':
            print("Shutting down server...")
            server_socket.close()
            break
        else:
            print("Invalid choice. Please try again.")
