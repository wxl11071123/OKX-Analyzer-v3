from dotenv import load_dotenv; load_dotenv('agent/.env')
import os

# Check context.py on the server
import subprocess
r = subprocess.run(['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10',
                    'root@8.141.108.66',
                    'grep -n "web_search" /root/OKX-Analyzer-v3/agent/src/agent/context.py'],
                   capture_output=True, text=True, timeout=15,
                   env={**os.environ, 'SSHPASS': '@Zhygar1937'})
print(r.stdout)

# Also check portfolio tool references
r2 = subprocess.run(['sshpass', '-p', '@Zhygar1937', 'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10',
                     'root@8.141.108.66',
                     'grep -n "okx_portfolio\|NEVER use web_search\|single source of truth" /root/OKX-Analyzer-v3/agent/src/agent/context.py'],
                    capture_output=True, text=True, timeout=15)
print(r2.stdout)
