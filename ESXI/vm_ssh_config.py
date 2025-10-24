"""
VM SSH configuration used by backend when SSH'ing into virtual machines.
Store username/password here. For production, consider reading from environment or a secret store.
"""

VM_SSH = {
    'user': 'switchpc1',
    'password': '1234567'
}
