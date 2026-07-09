import os
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import json

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default='user')  # user, admin
    status = db.Column(db.String(50), default='active')  # active, suspended, banned
    
    # OAuth and Verification Tokens
    github_token = db.Column(db.String(512), nullable=True)
    discord_id = db.Column(db.String(100), nullable=True)
    is_email_verified = db.Column(db.Boolean, default=False)
    two_factor_secret = db.Column(db.String(100), nullable=True)
    
    # Billing / Trial Management
    trial_started_at = db.Column(db.DateTime, default=datetime.utcnow)
    stripe_subscription_id = db.Column(db.String(255), nullable=True)
    stripe_customer_id = db.Column(db.String(255), nullable=True)
    premium_until = db.Column(db.DateTime, nullable=True)
    plan_tier = db.Column(db.String(50), default='trial') # trial, hobby, pro, enterprise

    projects = db.relationship('Project', backref='owner', lazy=True, cascade="all, delete-orphan")
    logs = db.relationship('AuditLog', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_trial_expired(self):
        if self.plan_tier != 'trial':
            return False
        if self.premium_until and self.premium_until > datetime.utcnow():
            return False
        return datetime.utcnow() > (self.trial_started_at + timedelta(days=7))

class Project(db.Model):
    __tablename__ = 'projects'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    repo_url = db.Column(db.String(255), nullable=False)
    branch = db.Column(db.String(100), default='main')
    build_command = db.Column(db.String(255), nullable=True)
    start_command = db.Column(db.String(255), nullable=True)
    env_vars_json = db.Column(db.Text, default='{}')
    runtime_detected = db.Column(db.String(50), default='Static')
    
    # Container Status Fields
    container_id = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), default='stopped') # pending, running, stopped, building, failed
    custom_domain = db.Column(db.String(255), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    deployments = db.relationship('Deployment', backref='project', lazy=True, cascade="all, delete-orphan")

    def get_env_vars(self):
        try:
            return json.loads(self.env_vars_json)
        except:
            return {}

    def set_env_vars(self, data):
        self.env_vars_json = json.dumps(data)

class Deployment(db.Model):
    __tablename__ = 'deployments'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    commit_hash = db.Column(db.String(100), default='Manual')
    status = db.Column(db.String(50), default='queued') # queued, building, success, failed
    logs = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(255), nullable=False)
    ip_address = db.Column(db.String(45), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
