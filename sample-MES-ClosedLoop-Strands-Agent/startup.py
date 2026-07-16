import datetime
import os
import venv
from pathlib import Path
import subprocess
import hashlib
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


PROJECT_DIR = Path(__file__).resolve().parent;
VENV_DIR = PROJECT_DIR / '.venv';
REQUIREMENTS = PROJECT_DIR / 'requirements.txt';
ENV = PROJECT_DIR / '.env';

def get_python_venv():
    # Windows python virtual environment location
    if(os.name == 'nt'):
        return VENV_DIR / "Scripts" / "python.exe";
    else:
        # Mac/Linux venv location
        return VENV_DIR / "bin" / "python";

# Validates API Key
def validate_api_key(api_key):
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        #Latest version according to Anthropic API documentation. Must be updated if API version changes in the future.
        "anthropic-version": "2023-06-01"
    }
    request = Request(url, headers=headers)
    try:
        with urlopen(request) as response:
            if response.status == 200:
                return True
    #Checks if API key is valid
    except HTTPError as e:
        print(f"HTTP Error: {e.code} - {e.reason}")
        if(e.code == 401):
            print("Invalid API key.")
            return False
        else:
            print("URLError: Unable to verify API key due to connection issues. Please check your internet connection and try again.")
            return None
    #Checks connection
    except URLError as e:
        print(f"URL Error: {e.reason}")
        return None
    
def check_env():
    if(not ENV.exists()):
        print("Error: .env not found. Creating....")
        with open(ENV, 'w') as f:
            user_key = prompt_api_key()
            f.write(f"ANTHROPIC_API_KEY={user_key}\n")
            f.write("MES_MODEL_ID=claude-sonnet-4-6\n")
            f.write("MES_MAX_TOKENS=4096\n")
            f.write("MES_TEMPERATURE=0.2\n")
    else:
        # Check if ANTHROPIC_API_KEY is present and valid
        original_lines, env_vars = parse_env_vars()
        updated_lines = []
        rewrite_env = False
        written_keys = set()
        validKey = validate_api_key(env_vars.get("ANTHROPIC_API_KEY", ""))
        if("ANTHROPIC_API_KEY" not in env_vars or not env_vars["ANTHROPIC_API_KEY"].startswith("sk-ant-") or len(env_vars["ANTHROPIC_API_KEY"]) < 20 or not validKey):
            if(validKey is None):
                print("Unable to verify API key due to connection issues. Please check your internet connection and try again.")
                print("Stopping....")
                exit(0)
            else:
                print("Error: ANTHROPIC_API_KEY not found or invalid in .env. Please update the .env file with a valid API key.")
                rewrite_env = True
                user_key = prompt_api_key()
                env_vars["ANTHROPIC_API_KEY"] = user_key
        if("MES_MODEL_ID" not in env_vars):
            env_vars["MES_MODEL_ID"] = "claude-sonnet-4-6"
            rewrite_env = True
        if("MES_MAX_TOKENS" not in env_vars):
            env_vars["MES_MAX_TOKENS"] = "4096"
            rewrite_env = True
        if("MES_TEMPERATURE" not in env_vars):
            env_vars["MES_TEMPERATURE"] = "0.2"
            rewrite_env = True
        # Write back to .env
        if rewrite_env:
            for line in original_lines:
                if line.strip().startswith("#") or "=" not in line or line.strip() == "":
                    updated_lines.append(line)
                    continue
                key = line.split('=',1)[0].strip()
                updated_lines.append(f"{key}={env_vars[key]}\n")
                written_keys.add(key)
            # Add any new keys that were not in the original .env
            for key, value in env_vars.items():
                if key not in written_keys:
                    updated_lines.append(f"{key}={value}\n")
            with open(ENV,'w') as env_file:
                env_file.writelines(updated_lines)
          
            
# parses .env into dictionary of variables
def parse_env_vars():
    env_vars = {}
    original_lines = []
    with open(ENV, 'r') as f:
        original_lines = f.readlines()
        for original_line in original_lines:
            # Ignore comments and lines without '='
            if "=" not in original_line or original_line.strip().startswith("#"):
                continue
            key, value = original_line.strip().split('=', 1)
            env_vars[key] = value
    return original_lines, env_vars
def prompt_api_key():
    user_key = input("Enter your ANTHROPIC_API_KEY (or type exit to quit): ").strip()
    if(user_key.lower() == "exit"):
        print("Exiting...")
        exit(0)
    # Validate API key
    validKey = validate_api_key(user_key)
    while(not user_key.startswith("sk-ant-") or len(user_key) < 20 or not validKey):
        if(validKey is None):
            print("Unable to verify API key due to connection issues. Please check your internet connection and try again.")
            print("Stopping....")
            exit(0)
        else:
            print("Invalid API key. Please try again.")
        user_key = input("Enter your ANTHROPIC_API_KEY (or type exit to quit): ").strip()
        if(user_key.lower() == "exit"):
            print("Exiting...")
            exit(0)
        validKey = validate_api_key(user_key)
    return user_key
# Installs requirements from requirements.txt
def install_requirements(python_venv):
    print("--Installing Requirements--")
    subprocess.run(
        [
         str(python_venv),
            "-m",
            "pip",
            "install",
            "-r",
            str(REQUIREMENTS)
        ],
        check=True
    );
    
def main():
    os.chdir(PROJECT_DIR);
    python_venv = get_python_venv();
    # rename incompaitble venv
    if VENV_DIR.exists() and not python_venv.exists():
        print("--Creating virtual environment--");
        # Timestamps to avoid collisions with previous backups
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        VENV_DIR.rename(VENV_DIR.with_suffix(f'.venv_backup{timestamp}'));
    print("--Checking environment--")
    check_env();
    # create virtual environment if it doesn't exist
    if not python_venv.exists():
        print("--Creating virtual environment--")
        venv.create(VENV_DIR, with_pip=True);
    
    #Hashes requirements.txt and checks for changes to avoid unnecessary reinstallation of packages
    with open (REQUIREMENTS, 'rb') as f:
        requirements_content = f.read()
        requirements_hash = hashlib.sha256(requirements_content).hexdigest()
    hash_file = VENV_DIR / 'requirements_hash.txt'
    if hash_file.exists():
        with open(hash_file, 'r+') as f:
            current_hash = f.read()
            if current_hash != requirements_hash:
                print("--Requirements changed, reinstalling packages--")
                install_requirements(python_venv)
                f.seek(0)
                f.write(requirements_hash)
                f.truncate()
    else:
        print("--Installing requirements for the first time--")
        install_requirements(python_venv)
        with open(hash_file, 'w') as f:
            f.write(requirements_hash)

   

    # run streamlit
    print("--Starting Streamlit--")
    subprocess.run(
        [
            str(python_venv),
            "-m",
            "streamlit",
            "run",
            "app.py"
        ],
        check=True
    );
    
if(__name__ == "__main__"):
    main();
       