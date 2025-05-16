import os
import subprocess
import shutil
import logging
import uuid
import json
from urllib.parse import quote
from google.cloud import storage
import re

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

WORK_BASE_DIR = "/tmp/workspaces"

def log_file_content(request_id, file_path, description=""):
    if not description:
        description = os.path.basename(file_path)
    try:
        with open(file_path, 'r') as f:
            content = f.read()
            log_message(request_id, f"--- Content of {description} ({file_path}) ---")
            print(content)
            log_message(request_id, f"--- End Content of {description} ---")
    except FileNotFoundError:
        log_message(request_id, f"File not found when trying to log content: {file_path}", error=True)
    except Exception as e:
        log_message(request_id, f"Error reading file {file_path} for logging: {e}", error=True)

def main():
    work_dir = None
    global global_log_buffer
    global_log_buffer = []
    web_deploy_file_name = "web_deploy.txt"
    web_deploy_project_id = "preview-stack"
    
    try:
        request_id = os.getenv("REQUEST_ID", str(uuid.uuid4()))
        azure_project_name = os.getenv("AZURE_PROJECT_NAME")
        azure_repo_names = json.loads(os.getenv("AZURE_REPO_NAMES", "[]"))
        azure_pat = os.getenv("AZURE_PAT")
        gcs_bucket_name = os.getenv("GCS_BUCKET_NAME")

        if not all([azure_project_name, azure_pat, azure_repo_names and isinstance(azure_repo_names, list) and len(azure_repo_names) > 0]):
            raise ValueError("AZURE_PROJECT_NAME, AZURE_REPO_NAMES (as JSON array), AZURE_PAT are required for mobilePreviewDeploy")
        
        site_id = extract_prefix(request_id)
        log_message(request_id, f"Using Site ID / Target Name: {site_id}")

        unique_id = str(uuid.uuid4())
        work_dir = os.path.join(WORK_BASE_DIR, unique_id)
        
        os.makedirs(work_dir, exist_ok=True)
        log_message(request_id, f"Workspace created: {work_dir}")

        log_message(request_id, f"[mobilePreviewDeploy] Deploying multiple Azure repos: {azure_repo_names}")
        service_account_path = setup_service_account(request_id, work_dir, azure_pat)
        activate_gcloud_auth(request_id, service_account_path, web_deploy_project_id)
        create_firebase_rc_file(request_id, work_dir, web_deploy_project_id)
        create_firebase_json_with_target(request_id, work_dir, site_id, azure_repo_names)

        # Prepare the public_staging_dir once before staging any repos
        public_staging_dir = os.path.join(work_dir, "deploy_staging", site_id)
        if os.path.exists(public_staging_dir):
            shutil.rmtree(public_staging_dir, ignore_errors=True)
        os.makedirs(public_staging_dir, exist_ok=True)

        # Clone, build, and stage for each repo
        project_repo_paths = []
        for repo_name in azure_repo_names:
            log_message(request_id, f"[mobilePreviewDeploy] Cloning and building repo: {repo_name}")
            project_repo_path = clone_project_repo(request_id, work_dir, azure_pat, azure_project_name, repo_name)
            project_repo_paths.append((repo_name, project_repo_path))
        build_flutter_app(request_id, work_dir, project_repo_paths)

        # Stage each build/web to the correct subdirectory
        for repo_name, project_repo_path in project_repo_paths:
            stage_files_for_deployment(request_id, work_dir, site_id, repo_name, project_repo_path)

        # Only create site and apply target after successful build and staging
        ensure_firebase_site_exists_create_only(request_id, site_id, web_deploy_project_id)
        apply_firebase_target(request_id, work_dir, site_id, site_id, web_deploy_project_id)

        deploy_with_firebase_cli(request_id, work_dir, site_id, web_deploy_project_id)
        log_file_path = os.path.join(work_dir, f"{request_id}_{web_deploy_file_name}")
        with open(log_file_path, "w") as f:
            f.write("\n".join(global_log_buffer))
        create_artifact_log_file(log_file_path, request_id)
        upload_to_gcs(log_file_path, f"{request_id}/{web_deploy_file_name}", gcs_bucket_name, web_deploy_project_id)
        log_message(request_id, f"[mobilePreviewDeploy] Deployment log uploaded to GCS: {request_id}/{web_deploy_file_name}")

    except Exception as e:
        log_message(request_id, f"Error: {str(e)}", error=True)
        import traceback
        logging.error(traceback.format_exc())
        raise
    finally:
        if work_dir and os.path.exists(work_dir):
            try:
                os.chdir("/")
                shutil.rmtree(work_dir, ignore_errors=False)
                log_message(request_id, f"Workspace cleaned up: {work_dir}")
            except OSError as cleanup_error:
                 log_message(request_id, f"Error during workspace cleanup: {cleanup_error}", error=True)

def create_artifact_log_file(file_path, request_id):
    work_dir = WORK_BASE_DIR;
    dest_path = os.path.join(work_dir, "artifact_logs.log")
    shutil.copy(file_path, dest_path)
    log_message(request_id, f"Artifact log file created: {dest_path}")
    
def extract_prefix(input_str):
    # Split the input string at the underscore
    parts = input_str.split('_')
    prefix = parts[0] if parts else input_str

    # Define a regex pattern to match UUIDs
    uuid_pattern = re.compile(
        r'^([0-9a-fA-F]{8})-([0-9a-fA-F]{4})-'
        r'([0-9a-fA-F]{4})-([0-9a-fA-F]{4})-'
        r'([0-9a-fA-F]{12})$'
    )

    match = uuid_pattern.match(prefix)
    if match:
        # Concatenate the first and second groups to form the site_id
        return match.group(1) + match.group(2) + match.group(3) + match.group(4)
    else:
        # For non-UUID formats, return the entire prefix
        return prefix

def setup_service_account(request_id, work_dir, azure_pat):
    log_message(request_id, "[setup_service_account] Setting up service account...")
    sa_repo_dir = os.path.join(work_dir, "service-account-repo")
    sa_key_filename = "preview-stack-service-account.json"
    target_key_path = os.path.join(work_dir, sa_key_filename)

    if os.path.exists(sa_repo_dir):
        shutil.rmtree(sa_repo_dir, ignore_errors=True)

    clone_url = f"https://{azure_pat}@dev.azure.com/zpqv/zpqv-ai/_git/service-accounts"
    log_message(request_id, f"[setup_service_account] Cloning service account repo...")
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--single-branch", "--branch", "master", clone_url, sa_repo_dir], check=True, capture_output=True, text=True)
        log_message(request_id, "[setup_service_account] Service account repo cloned.")
    except subprocess.CalledProcessError as e:
         log_message(request_id, f"[setup_service_account] Git clone failed: {e.stderr}", error=True)
         raise

    source_key_path = os.path.join(sa_repo_dir, sa_key_filename)
    if not os.path.exists(source_key_path):
        raise FileNotFoundError(f"{sa_key_filename} not found in the service account repo at {sa_repo_dir}")

    shutil.copy(source_key_path, target_key_path)
    log_message(request_id, f"[setup_service_account] Service account key copied to {target_key_path}")
    log_file_content(request_id, target_key_path, "Service Account Key")

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = target_key_path
    log_message(request_id, f"[setup_service_account] Set os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = {target_key_path}")

    shutil.rmtree(sa_repo_dir, ignore_errors=True)
    return target_key_path

def activate_gcloud_auth(request_id, service_account_path, web_deploy_project_id):
    log_message(request_id, "[activate_gcloud_auth] Activating service account for gcloud...")
    try:
        subprocess.run(["gcloud", "auth", "activate-service-account", "--key-file", service_account_path, "--quiet"], check=True, capture_output=True, text=True)
        log_message(request_id, "[activate_gcloud_auth] gcloud auth activated.")
        subprocess.run(["gcloud", "config", "set", "project", web_deploy_project_id, "--quiet"], check=True, capture_output=True, text=True)
        log_message(request_id, f"[activate_gcloud_auth] gcloud project set to {web_deploy_project_id}.")
    except subprocess.CalledProcessError as e:
        log_message(request_id, f"[activate_gcloud_auth] gcloud command failed: {e.stderr}", error=True)
        raise

def create_firebase_rc_file(request_id, work_dir, web_deploy_project_id):
    firebaserc_data = {"projects": {"default": web_deploy_project_id}}
    firebaserc_path = os.path.join(work_dir, ".firebaserc")
    try:
        with open(firebaserc_path, "w") as f:
            json.dump(firebaserc_data, f, indent=2)
        log_message(request_id, f"[create_firebase_rc_file] .firebaserc created for project {web_deploy_project_id} at {firebaserc_path}")
        log_file_content(request_id, firebaserc_path, ".firebaserc")
    except IOError as e:
        log_message(request_id, f"[create_firebase_rc_file] Error creating .firebaserc: {e}", error=True)
        raise

def create_firebase_json_with_target(request_id, work_dir, target_name, azure_repo_names):
    log_message(request_id, f"[create_firebase_json_with_target] Creating firebase.json for target: {target_name}")
    public_dir = f"deploy_staging/{target_name}"
    rewrites = []
    for repo_name in azure_repo_names:
        rewrites.append({
            "source": f"/{repo_name}{{,/**}}",
            "destination": f"/{repo_name}/index.html"
        })
    firebase_json_content = {
        "hosting": [
            {
                "target": target_name,
                "public": public_dir,
                "ignore": ["firebase.json", "**/.*", "**/node_modules/**"],
                "rewrites": rewrites
            }
        ]
    }
    firebase_json_path = os.path.join(work_dir, "firebase.json")
    try:
        with open(firebase_json_path, "w") as f:
            json.dump(firebase_json_content, f, indent=2)
        log_message(request_id, f"[create_firebase_json_with_target] firebase.json created at {firebase_json_path}")
        log_file_content(request_id, firebase_json_path, "firebase.json")
    except IOError as e:
        log_message(request_id, f"[create_firebase_json_with_target] Error creating firebase.json: {e}", error=True)
        raise

def ensure_firebase_site_exists_create_only(request_id, site_id, web_deploy_project_id):
    log_message(request_id, f"[ensure_firebase_site_exists_create_only] Ensuring Firebase site exists (by attempting create): {site_id} in project {web_deploy_project_id}")
    try:
        create_cmd = ["firebase", "hosting:sites:create", site_id, f"--project={web_deploy_project_id}"]
        log_message(request_id, f"[ensure_firebase_site_exists_create_only] Running command: {' '.join(create_cmd)}")
        current_env = os.environ.copy()
        sa_path_check = current_env.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not sa_path_check:
             log_message(request_id, "[ensure_firebase_site_exists_create_only] ENV VAR CHECK FAILED: GOOGLE_APPLICATION_CREDENTIALS not found in current_env!", error=True)
             raise ValueError("Service account path env var missing unexpectedly before firebase command.")
        else:
             log_message(request_id, f"[ensure_firebase_site_exists_create_only] ENV VAR CHECK OK: Found GOOGLE_APPLICATION_CREDENTIALS={sa_path_check}")

        create_result = subprocess.run(create_cmd, check=False, capture_output=True, text=True, cwd=WORK_BASE_DIR, env=current_env)
        log_message(request_id, f"[ensure_firebase_site_exists_create_only] Create command stdout: {create_result.stdout}")
        log_message(request_id, f"[ensure_firebase_site_exists_create_only] Create command stderr: {create_result.stderr}")

        if create_result.returncode == 0:
            log_message(request_id, f"[ensure_firebase_site_exists_create_only] Site {site_id} created successfully.")
        else:
            log_message(request_id, f"[ensure_firebase_site_exists_create_only] Site {site_id} already exists (HTTP 409 received, ignoring as expected).")
    except FileNotFoundError:
        log_message(request_id, "Error: 'firebase' command not found. Ensure firebase-tools is installed and in PATH.", error=True)
        raise
    except Exception as e:
        log_message(request_id, f"An unexpected error occurred during site creation attempt: {e}", error=True)
        raise

def apply_firebase_target(request_id, work_dir, target_name, site_id, web_deploy_project_id):
    log_message(request_id, f"[apply_firebase_target] Mapping target '{target_name}' to site '{site_id}' in project {web_deploy_project_id}")
    try:
        apply_cmd = ["firebase", "target:apply", "hosting", target_name, site_id, f"--project={web_deploy_project_id}"]
        log_message(request_id, f"[apply_firebase_target] Running command: {' '.join(apply_cmd)} from CWD: {work_dir}") 

        current_env = os.environ.copy()
        sa_path_check = current_env.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not sa_path_check:
             log_message(request_id, "[apply_firebase_target] ENV VAR CHECK FAILED: GOOGLE_APPLICATION_CREDENTIALS not found in current_env!", error=True)
             raise ValueError("Service account path env var missing unexpectedly before firebase command.")

        apply_result = subprocess.run(apply_cmd, check=True, capture_output=True, text=True, cwd=work_dir, env=current_env)

        log_message(request_id, f"[apply_firebase_target] Apply command stdout: {apply_result.stdout}")
        if apply_result.stderr:
             log_message(request_id, f"[apply_firebase_target] Apply command stderr: {apply_result.stderr}")
        log_message(request_id, f"[apply_firebase_target] Target '{target_name}' successfully mapped to site '{site_id}'.")

    except FileNotFoundError:
        log_message(request_id, "[apply_firebase_target] Error: 'firebase' command not found.", error=True)
        raise
    except subprocess.CalledProcessError as e:
        log_message(request_id, f"[apply_firebase_target] Firebase target:apply command failed. Return code: {e.returncode}", error=True)
        log_message(request_id, f"[apply_firebase_target] stdout: {e.stdout}", error=True)
        log_message(request_id, f"[apply_firebase_target] stderr: {e.stderr}", error=True)
        raise
    except Exception as e:
        log_message(request_id, f"[apply_firebase_target] An unexpected error occurred during target apply: {e}", error=True)
        raise

def clone_project_repo(request_id, work_dir, azure_pat, azure_project_name, azure_repo_name):
    log_message(request_id, "[clone_project_repo] Cloning project repo...")
    project_repo_path = os.path.join(work_dir, azure_repo_name)
    if os.path.exists(project_repo_path):
        shutil.rmtree(project_repo_path, ignore_errors=True)

    encoded_project_name = quote(azure_project_name, safe='')
    encoded_repo_name = quote(azure_repo_name, safe='')
    clone_url = f"https://{azure_pat}@dev.azure.com/zpqv/{encoded_project_name}/_git/{encoded_repo_name}"
    log_message(request_id, f"[clone_project_repo] Cloning from {clone_url} into {project_repo_path}")
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--single-branch", "--branch", "master", clone_url, project_repo_path], check=True, capture_output=True, text=True)
        log_message(request_id, "[clone_project_repo] Project repo cloned.")
        return project_repo_path
    except subprocess.CalledProcessError as e:
        log_message(request_id, f"[clone_project_repo] Git clone failed: {e.stderr}", error=True)
        raise

def build_flutter_app(request_id, work_dir, project_repo_paths):
    for repo_name, flutter_project_path in project_repo_paths:
        log_message(request_id, f"[build_flutter_app] Building Flutter web app for repo '{repo_name}'...")
        pubspec_path = os.path.join(flutter_project_path, "pubspec.yaml")
        web_index_path = os.path.join(flutter_project_path, "web", "index.html")
        log_message(request_id, f"[build_flutter_app] Checking Flutter project path: {flutter_project_path}")
        log_message(request_id, f"[build_flutter_app] Checking for pubspec.yaml at: {pubspec_path} (Exists: {os.path.exists(pubspec_path)})")
        log_message(request_id, f"[build_flutter_app] Checking for web/index.html at: {web_index_path} (Exists: {os.path.exists(web_index_path)})")
        try:
            log_message(request_id, f"[build_flutter_app] Listing contents of {flutter_project_path}:")
            list_cmd = ["ls", "-la", flutter_project_path]
            list_web_cmd = ["ls", "-la", os.path.join(flutter_project_path, "web")]
            subprocess.run(list_cmd, check=False)
            log_message(request_id, f"[build_flutter_app] Listing contents of {os.path.join(flutter_project_path, 'web')}:")
            subprocess.run(list_web_cmd, check=False)
        except Exception as list_err:
            log_message(request_id, f"[build_flutter_app] Error listing directory contents: {list_err}")

        if not os.path.exists(pubspec_path):
            raise FileNotFoundError(f"pubspec.yaml not found in assumed project path {flutter_project_path}")
        if not os.path.exists(web_index_path):
            log_message(request_id, f"[build_flutter_app] CRITICAL: web/index.html is missing in the source code at {web_index_path}. Flutter build cannot proceed.", error=True)
            raise FileNotFoundError(f"web/index.html not found in the cloned project at {flutter_project_path}. Cannot build.")

        try:
            log_message(request_id, f"[build_flutter_app] Running 'flutter pub get' for repo '{repo_name}'...")
            pub_get_result = subprocess.run(["flutter", "pub", "get"], check=True, cwd=flutter_project_path, capture_output=True, text=True)
            log_message(request_id, f"[build_flutter_app] 'flutter pub get' completed for '{repo_name}'. Output:\n{pub_get_result.stdout}")
            if pub_get_result.stderr:
                log_message(request_id, f"[build_flutter_app] 'flutter pub get' stderr for '{repo_name}':\n{pub_get_result.stderr}")

            log_message(request_id, f"[build_flutter_app] Running 'flutter build web' for '{repo_name}'...")
            build_cmd = ["flutter", "build", "web", "--release", "--no-tree-shake-icons", f"--base-href=/{repo_name}/"]
            build_result = subprocess.run(build_cmd, check=True, cwd=flutter_project_path, capture_output=True, text=True)
            log_message(request_id, f"[build_flutter_app] Flutter build completed for '{repo_name}'. Output:\n{build_result.stdout}")
            if build_result.stderr:
                log_message(request_id, f"[build_flutter_app] Flutter build stderr for '{repo_name}':\n{build_result.stderr}")
        except FileNotFoundError:
            log_message(request_id, f"[build_flutter_app] Error: 'flutter' command not found. Ensure Flutter SDK is installed and in PATH.", error=True)
            raise
        except subprocess.CalledProcessError as e:
            log_message(request_id, f"[build_flutter_app] Flutter command failed for '{repo_name}': {e.cmd}", error=True)
            log_message(request_id, f"[build_flutter_app] Return Code: {e.returncode}", error=True)
            log_message(request_id, f"[build_flutter_app] stdout:\n{e.stdout}", error=True)
            log_message(request_id, f"[build_flutter_app] stderr:\n{e.stderr}", error=True)
            raise

def stage_files_for_deployment(request_id, work_dir, site_id, azure_repo_name, project_repo_path):
    log_message(request_id, "[stage_files_for_deployment] Staging build artifacts...")
    public_staging_dir = os.path.join(work_dir, "deploy_staging", site_id)
    app_staging_path = os.path.join(public_staging_dir, azure_repo_name)
    build_output_path = os.path.join(project_repo_path, "build", "web")

    # No longer delete or recreate public_staging_dir here; it is handled once in main()
    os.makedirs(app_staging_path, exist_ok=True)
    log_message(request_id, f"[stage_files_for_deployment] Staging directory created: {public_staging_dir}")

    if not os.path.exists(build_output_path):
         raise FileNotFoundError(f"Build output not found at {build_output_path}. Check if flutter build completed successfully.")

    try:
        shutil.copytree(build_output_path, app_staging_path, dirs_exist_ok=True)
        log_message(request_id, f"[stage_files_for_deployment] Copied files from {build_output_path} to {app_staging_path}")
        log_message(request_id, f"[stage_files_for_deployment] Staging dir '{public_staging_dir}' contents: {os.listdir(public_staging_dir)}")
        log_message(request_id, f"[stage_files_for_deployment] App staging dir '{app_staging_path}' contents: {os.listdir(app_staging_path)}")
    except Exception as e:
        log_message(request_id, f"[stage_files_for_deployment] Error staging files: {e}", error=True)
        raise

def deploy_with_firebase_cli(request_id, work_dir, target_name, web_deploy_project_id):
    log_message(request_id, f"[deploy_with_firebase_cli] Deploying target {target_name} using firebase-tools...")
    deploy_cmd = ["firebase", "deploy", f"--project={web_deploy_project_id}", f"--only=hosting:{target_name}", "--force"]

    log_message(request_id, f"[deploy_with_firebase_cli] Running command: {' '.join(deploy_cmd)} from CWD: {work_dir}")
    try:
        current_env = os.environ.copy()
        sa_path_check = current_env.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not sa_path_check:
             log_message(request_id, "[deploy_with_firebase_cli] ENV VAR CHECK FAILED: GOOGLE_APPLICATION_CREDENTIALS not found in current_env!", error=True)
             raise ValueError("Service account path env var missing unexpectedly before firebase command.")

        result = subprocess.run(deploy_cmd, check=True, cwd=work_dir, capture_output=True, text=True, env=current_env)

        log_message(request_id, f"[deploy_with_firebase_cli] firebase deploy stdout: {result.stdout}")
        if result.stderr:
             log_message(request_id, f"[deploy_with_firebase_cli] firebase deploy stderr: {result.stderr}")
        log_message(request_id, f"[deploy_with_firebase_cli] Deployment successful for target {target_name}.")

    except FileNotFoundError:
        log_message(request_id, "[deploy_with_firebase_cli] Error: 'firebase' command not found.", error=True)
        raise
    except subprocess.CalledProcessError as e:
        log_message(request_id, f"[deploy_with_firebase_cli] firebase deploy command failed. Return code: {e.returncode}", error=True)
        log_message(request_id, f"[deploy_with_firebase_cli] stdout: {e.stdout}", error=True)
        log_message(request_id, f"[deploy_with_firebase_cli] stderr: {e.stderr}", error=True)
        raise
    except Exception as e:
        log_message(request_id, f"[deploy_with_firebase_cli] An unexpected error occurred during firebase deploy: {e}", error=True)
        raise

def upload_to_gcs(local_file_path, gcs_file_name, gcs_bucket_name, web_deploy_project_id):
    try:
        client = storage.Client(project=web_deploy_project_id)
        bucket = client.bucket(gcs_bucket_name)
        blob = bucket.blob(gcs_file_name)
        blob.upload_from_filename(local_file_path)
        logging.info(f"Uploaded {local_file_path} to gs://{gcs_bucket_name}/{gcs_file_name}")
    except Exception as e:
        logging.error(f"Failed to upload {local_file_path} to GCS bucket {gcs_bucket_name}: {e}")
        raise

def log_message(request_id, message, error=False):
    global global_log_buffer
    global_log_buffer.append(f"[{request_id}] {message}")
    if error:
        logging.error(f"[{request_id}] {message}")
    else:
        logging.info(f"[{request_id}] {message}")

def find_flutter_project_dir(path):
    for root, dirs, files in os.walk(path):
        if "pubspec.yaml" in files:
            return root
    raise FileNotFoundError(f"Flutter project (pubspec.yaml) not found within {path}")

if __name__ == "__main__":
    main()
