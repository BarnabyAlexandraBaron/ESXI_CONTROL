ESXi Inventory UI

How to run:

1) Create a Python venv (optional) and install requirements:

   python -m venv .venv
   .\.venv\Scripts\pip.exe install -r ..\ESXI\requirements.txt

2) Start the Flask API from `h:\Script\ESXI`:

   python web_api.py

3) Open the UI in browser:

   http://localhost:5000/

Notes:
- The UI reads from the local SQLite DB `esxi_data.db` in the same folder as `web_api.py`.
- The UI is a lightweight Vue 3 SPA served statically by Flask; no build step required.
