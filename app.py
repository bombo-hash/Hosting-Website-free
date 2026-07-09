import os
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_init, render_template, request, jsonify, session, redirect, url_for
from models import db, User, Project, Deployment, AuditLog
from engine import CloudEngine

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'vain-cloud-super-signature-key-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://postgres:postgres@db:5432/vaincloud')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
engine = CloudEngine()

# --- Background Infrastructure Loop Handler ---
def process_system_reconciliation_loop():
    """
    Background worker process. Keeps apps running, manages trials, 
    and handles state management safely away from request contexts.
    """
    with app.app_context():
        while True:
            try:
                # 1. Evaluate Trial Accounts & Handle Feature Gates
                users = User.query.filter_by(plan_tier='trial').all()
                for user in users:
                    if user.is_trial_expired:
                        for project in user.projects:
                            if project.status == 'running':
                                engine.terminate_container(project.container_id)
                                project.status = 'stopped'
                                log = AuditLog(user_id=user.id, action=f"Project {project.name} auto-stopped: Trial Expired")
                                db.session.add(log)
                
                # 2. Reconcile Build Queue Pipelines
                queued_deployments = Deployment.query.filter_by(status='queued').all()
                for deploy in queued_deployments:
                    project = deploy.project
                    project.status = 'building'
                    deploy.status = 'building'
                    db.session.commit()
                    
                    status, container_id, logs = engine.provision_application(
                        project_id=project.id,
                        repo_url=project.repo_url,
                        branch=project.branch,
                        env_vars=project.get_env_vars(),
                        build_cmd=project.build_command,
                        start_cmd=project.start_command
                    )
                    
                    project.container_id = container_id
                    project.status = 'running' if status == 'success' else 'failed'
                    deploy.status = status
                    deploy.logs = logs
                    db.session.commit()
                    
            except Exception as e:
                print(f"Error checking platform background schedules: {e}")
            time.sleep(10)

# Start background thread execution immediately
threading.Thread(target=process_system_reconciliation_loop, daemon=True).start()

# --- Security/Session Helper Interceptors ---
@app.before_request
def enforce_security_context():
    if not hasattr(app, '_db_initialized'):
        db.create_all()
        # Seed default administrative credentials if absent
        if not User.query.filter_by(role='admin').first():
            root_admin = User(email="admin@vaincloud.local", role="admin", is_email_verified=True, plan_tier="enterprise")
            root_admin.set_password("VainCloudAdmin2026!")
            db.session.add(root_admin)
            db.session.commit()
        app._db_initialized = True

# --- API Layer Routes ---
@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    if User.query.filter_by(email=data.get('email')).first():
        return jsonify({"error": "Account identifier already exists"}), 400
    user = User(email=data.get('email'))
    user.set_password(data.get('password'))
    db.session.add(user)
    db.session.commit()
    return jsonify({"message": "Registration successful"}), 201

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    user = User.query.filter_by(email=data.get('email')).first()
    if not user or not user.check_password(data.get('password')):
        return jsonify({"error": "Invalid authentication credentials"}), 401
    if user.status in ['suspended', 'banned']:
        return jsonify({"error": "Account access has been terminated by administrator"}), 403
        
    session['user_id'] = user.id
    session['role'] = user.role
    return jsonify({"message": "Authentication token context granted", "role": user.role})

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"message": "Session cleared successfully"})

@app.route('/api/projects', methods=['GET', 'POST'])
def manage_projects():
    if 'user_id' not in session: return jsonify({"error": "Unauthorized"}), 401
    user = User.query.get(session['user_id'])
    
    if request.method == 'GET':
        projects = Project.query.filter_by(user_id=user.id).all()
        output = []
        for p in projects:
            metrics = engine.get_container_metrics(p.container_id) if p.status == 'running' else {"cpu":0,"memory":0}
            output.append({
                "id": p.id, "name": p.name, "repo_url": p.repo_url, "branch": p.branch,
                "status": p.status, "runtime": p.runtime_detected, "metrics": metrics,
                "domain": p.custom_domain or f"{p.name}.vaincloud.local"
            })
        return jsonify({"projects": output, "trial_expired": user.is_trial_expired})

    if user.is_trial_expired:
        return jsonify({"error": "Your free trial usage cycle has expired. Upgrade your billing tier."}), 402

    data = request.get_json() or {}
    project = Project(
        user_id=user.id, name=data.get('name'), repo_url=data.get('repo_url'),
        branch=data.get('branch', 'main'), build_command=data.get('build_cmd'),
        start_command=data.get('start_cmd')
    )
    project.set_env_vars(data.get('env_vars', {}))
    db.session.add(project)
    db.session.commit()

    deployment = Deployment(project_id=project.id, status='queued')
    db.session.add(deployment)
    db.session.commit()
    return jsonify({"message": "Deployment workflow initialized successfully"}), 202

@app.route('/api/projects/<int:pid>/action', methods=['POST'])
def project_lifecycle_action():
    if 'user_id' not in session: return jsonify({"error": "Unauthorized"}), 401
    project = Project.query.filter_by(id=pid, user_id=session['user_id']).first_or_404()
    action = request.get_json().get('action')

    if action == 'stop' and project.status == 'running':
        engine.terminate_container(project.container_id)
        project.status = 'stopped'
    elif action == 'start' and project.status == 'stopped':
        deployment = Deployment(project_id=project.id, status='queued')
        db.session.add(deployment)
        project.status = 'pending'
    elif action == 'delete':
        engine.terminate_container(project.container_id)
        db.session.delete(project)
    
    db.session.commit()
    return jsonify({"message": f"Action {action} processed successfully"})

@app.route('/api/projects/<int:pid>/logs', methods=['GET'])
def get_project_logs():
    if 'user_id' not in session: return jsonify({"error": "Unauthorized"}), 401
    project = Project.query.filter_by(id=pid, user_id=session['user_id']).first_or_404()
    latest_deploy = Deployment.query.filter_by(project_id=project.id).order_by(Deployment.created_at.desc()).first()
    return jsonify({
        "logs": latest_deploy.logs if latest_deploy else "No execution sequences found for the target environment deployment profile."
    })

# --- Admin Operations Layer ---
@app.route('/api/admin/metrics', methods=['GET'])
def admin_metrics():
    if session.get('role') != 'admin': return jsonify({"error": "Access Denied"}), 403
    sys_metrics = engine.get_system_metrics()
    total_users = User.query.count()
    total_apps = Project.query.count()
    return jsonify({"system": sys_metrics, "users_count": total_users, "apps_count": total_apps})

@app.route('/api/admin/users', methods=['GET', 'POST'])
def admin_manage_users():
    if session.get('role') != 'admin': return jsonify({"error": "Access Denied"}), 403
    if request.method == 'GET':
        users = User.query.all()
        return jsonify({"users": [{
            "id": u.id, "email": u.email, "role": u.role, "status": u.status, "plan_tier": u.plan_tier
        } for u in users]})
    
    data = request.get_json()
    target_user = User.query.get(data.get('user_id'))
    if target_user:
        if data.get('action') == 'suspend': target_user.status = 'suspended'
        if data.get('action') == 'activate': target_user.status = 'active'
        if data.get('action') == 'grant_premium':
            target_user.plan_tier = 'pro'
            target_user.premium_until = datetime.utcnow() + timedelta(days=int(data.get('days', 30)))
        db.session.commit()
    return jsonify({"message": "User contextual state updated successfully"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
