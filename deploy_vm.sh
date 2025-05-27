cat <<EOT > deploy_vm.sh
#!/bin/bash
# Deploy LLM server on a new VM
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 server.py
EOT
