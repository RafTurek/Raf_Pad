# app.py
from flask import Flask, render_template, request, jsonify
from models import db, Project, Note, Task, Subtask
from datetime import datetime, UTC
from markupsafe import escape
from hmac import compare_digest
import bleach
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from typing import List, Dict
from dataclasses import dataclass, field
import json
import random
from backend.inference import LLMInference
import os

# Import ChatManager from the new module
from core.chat_manager import ChatManager

app = Flask(__name__)

# Konfiguracja bazy danych
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///rafpad.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


# Inicjalizacja bazy danych
db.init_app(app)

# Dodaj rate limiting z poprawną konfiguracją
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="memory://",  # Explicit storage configuration
    default_limits=["200 per day", "50 per hour"]
)

# Klasa do przechowywania historii czatu
@dataclass
class ChatMessage:
    role: str  # 'user' lub 'assistant'
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))  # Używaj timezone-aware
    
    def to_dict(self):
        return {
            'role': self.role,
            'content': self.content,
            'timestamp': self.timestamp.isoformat()
        }

# Inicjalizacja chat managera
chat_manager = ChatManager()

# Zmień before_first_request na before_request z flagą
_is_first_request = True

@app.before_request
def initialize_on_first_request():
    global _is_first_request
    if _is_first_request:
        # Removed initialize_model call since it's no longer needed
        _is_first_request = False

@app.route('/')
def index():
    return render_template('index.html')

# API dla projektów
@app.route('/api/projects', methods=['GET'])
def get_projects():
    projects = Project.query.all()
    return jsonify([{
        'id': p.id,
        'tag': p.tag,
        'name': p.name
    } for p in projects])

@app.route('/api/projects', methods=['POST'])
def create_project():
    data = request.json
    project = Project(tag=data['tag'], name=data.get('name'))
    db.session.add(project)
    try:
        db.session.commit()
        return jsonify({'id': project.id, 'tag': project.tag, 'name': project.name}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# API dla notatek
@app.route('/api/notes', methods=['GET'])
def get_notes():
    notes = Note.query.filter_by(deleted_at=None).all()
    return jsonify([{
        'id': n.id,
        'content': n.content,
        'category': n.category,
        'project_tag': n.project.tag,
        'created_at': n.created_at.isoformat(),
        'updated_at': n.updated_at.isoformat()
    } for n in notes])

@app.route('/api/notes', methods=['POST'])
@limiter.limit("100 per day")
def create_note():
    try:
        data = request.json
        content = sanitize_input(data.get('content', ''))
        
        # Znajdź projekt po tagu lub stwórz nowy
        project_tag = data.get('project_tag', '#inbox')
        project = Project.query.filter_by(tag=project_tag).first()
        if not project and project_tag != '#inbox':
            # Stwórz nowy projekt
            project = Project(tag=project_tag, name=project_tag.replace('#', ''))
            db.session.add(project)
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                return jsonify({'error': f'Failed to create project: {str(e)}'}), 400
        
        if not project:
            project = Project.query.filter_by(tag='#inbox').first()
        
        note = Note(
            content=content,
            category=data.get('category'),
            project_id=project.id
        )
        
        db.session.add(note)
        try:
            db.session.commit()
            return jsonify({
                'id': note.id,
                'content': note.content,
                'category': note.category,
                'project_tag': note.project.tag,
                'created_at': note.created_at.isoformat()
            }), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

# API dla zadań
@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    tasks = Task.query.filter_by(deleted_at=None).all()
    return jsonify([{
        'id': t.id,
        'content': t.content,
        'category': t.category,
        'priority': t.priority,
        'deadline': t.deadline.isoformat() if t.deadline else None,
        'is_completed': t.is_completed,
        'project_tag': t.project.tag,
        'created_at': t.created_at.isoformat(),
        'updated_at': t.updated_at.isoformat(),
        'subtasks': [{
            'id': s.id,
            'content': s.content,
            'is_completed': s.is_completed
        } for s in t.subtasks]
    } for t in tasks])

@app.route('/api/tasks', methods=['POST'])
def create_task():
    data = request.json
    
    # Znajdź projekt po tagu lub stwórz nowy
    project_tag = data.get('project_tag', '#inbox')
    project = Project.query.filter_by(tag=project_tag).first()
    if not project and project_tag != '#inbox':
        # Stwórz nowy projekt
        project = Project(tag=project_tag, name=project_tag.replace('#', ''))
        db.session.add(project)
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Failed to create project: {str(e)}'}), 400
    
    if not project:
        project = Project.query.filter_by(tag='#inbox').first()
    
    # Konwersja deadline string na datetime jeśli istnieje
    deadline = None
    if data.get('deadline'):
        try:
            deadline = datetime.fromisoformat(data['deadline'])
        except ValueError:
            return jsonify({'error': 'Invalid deadline format'}), 400
    
    task = Task(
        content=data['content'],
        category=data.get('category'),
        priority=data.get('priority'),
        deadline=deadline,
        project_id=project.id
    )
    
    # Dodaj podzadania jeśli istnieją
    if 'subtasks' in data:
        for subtask_data in data['subtasks']:
            subtask = Subtask(content=subtask_data['content'])
            task.subtasks.append(subtask)
    
    db.session.add(task)
    try:
        db.session.commit()
        return jsonify({
            'id': task.id,
            'content': task.content,
            'category': task.category,
            'priority': task.priority,
            'deadline': task.deadline.isoformat() if task.deadline else None,
            'project_tag': task.project.tag,
            'created_at': task.created_at.isoformat(),
            'subtasks': [{
                'id': s.id,
                'content': s.content,
                'is_completed': s.is_completed
            } for s in task.subtasks]
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/add_ai_task', methods=['POST'])
def add_ai_task():
    '''
    Endpoint to add new task created by AI (LLM).
    Recevies a prompt in JSON format, uses LLM to generate task content,
    then adds the task to the database.
    '''
    task_data = request.json
    if not task_data or 'prompt' not in task_data:
        return jsonify({'error': 'Prompt is required'}), 400
    
    prompt = task_data['prompt']
    generated_content = chat_manager.generate_task_content_with_LLM(prompt)
    if not generated_content:
        return jsonify({'error': 'Failed to generate task content'}), 500
    
    project_tag = task_data.get('project_tag', '#inbox') #default project
    category = task_data.get('category', '')
    priority = task_data.get('priority', '')
    deadline_str = task_data.get('deadline', None) # Deadline as string from JSON
    deadline = None # Initialize deadline as None

    #Converts string deadline to datetime object
    if deadline_str:
        try:
            deadline = datetime.fromisoformat(deadline_str)
        except ValueError:
            return jsonify({'error': 'Invalid deadline format'}), 400
    
    # Search for project with given project_tag or make a new one if it doesn't exist (if project_tag is not #inbox)
    project = Project.query.filter_by(tag=project_tag).first()
    if not project and project_tag != '#inbox':
        project = Project(tag=project_tag, name=project_tag.replace('#', ''))
        db.session.add(project)
        try:
            db.session.commit() # Commit the project to the database to retrieve its ID
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Failed to create project: {str(e)}'}), 400
    
    if not project: # If project still doesn't exist (for example #imbox not found)
        project = Project.query.filter_by(tag='#inbox').first()
        if not project: # Emergency create #inbox project
            project = Project(tag='#inbox', name='Inbox')
            db.session.add(project)
            db.session.commit()

    # Create new task with generated content    
    task = Task(
        content=generated_content,
        category=category,
        priority=priority,
        deadline=deadline,
        project_id=project.id # Use the project ID to link the task to the project

    )

    db.session.add(task)
    try:
        db.session.commit()
        return jsonify({
            'message': 'AI task added successfully',
            'task_id': task.id # Return the task ID to the client
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400



# Endpoint do przenoszenia notatek między projektami
@app.route('/api/notes/<int:note_id>/move', methods=['PATCH'])
def move_note(note_id):
    data = request.json
    new_project_tag = data.get('project_tag')
    
    if not new_project_tag:
        return jsonify({'error': 'New project tag is required'}), 400
        
    note = Note.query.get_or_404(note_id)
    new_project = Project.query.filter_by(tag=new_project_tag).first()
    
    if not new_project:
        return jsonify({'error': 'Target project not found'}), 404
        
    note.project_id = new_project.id
    note.updated_at = datetime.utcnow()
    
    try:
        db.session.commit()
        return jsonify({
            'id': note.id,
            'content': note.content,
            'category': note.category,
            'project_tag': note.project.tag,
            'updated_at': note.updated_at.isoformat()
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# Endpoint do przenoszenia zadań między projektami
@app.route('/api/tasks/<int:task_id>/move', methods=['PATCH'])
def move_task(task_id):
    data = request.json
    new_project_tag = data.get('project_tag')
    
    if not new_project_tag:
        return jsonify({'error': 'New project tag is required'}), 400
        
    task = Task.query.get_or_404(task_id)
    new_project = Project.query.filter_by(tag=new_project_tag).first()
    
    if not new_project:
        return jsonify({'error': 'Target project not found'}), 404
        
    task.project_id = new_project.id
    task.updated_at = datetime.utcnow()
    
    try:
        db.session.commit()
        return jsonify({
            'id': task.id,
            'content': task.content,
            'category': task.category,
            'priority': task.priority,
            'project_tag': task.project.tag,
            'updated_at': task.updated_at.isoformat()
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# Endpoint do soft delete
@app.route('/api/<string:element_type>/<int:element_id>', methods=['DELETE'])
def soft_delete_element(element_type, element_id):
    model_map = {
        'notes': Note,
        'tasks': Task,
        'subtasks': Subtask
    }
    
    if element_type not in model_map:
        return jsonify({'error': 'Invalid element type'}), 400
        
    Model = model_map[element_type]
    element = Model.query.get_or_404(element_id)
    
    element.deleted_at = datetime.utcnow()
    
    try:
        db.session.commit()
        return jsonify({'message': f'{element_type} soft deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# Endpoint do pobierania pojedynczej notatki
@app.route('/api/notes/<int:note_id>', methods=['GET'])
def get_note(note_id):
    note = Note.query.get_or_404(note_id)
    return jsonify({
        'id': note.id,
        'content': note.content,
        'category': note.category,
        'project_tag': note.project.tag,
        'created_at': note.created_at.isoformat(),
        'updated_at': note.updated_at.isoformat()
    })

# Endpoint do aktualizacji notatki
@app.route('/api/notes/<int:note_id>', methods=['PUT'])
def update_note(note_id):
    note = Note.query.get_or_404(note_id)
    data = request.json
    
    # Znajdź projekt po tagu lub zostaw obecny
    if 'project_tag' in data:
        project = Project.query.filter_by(tag=data['project_tag']).first()
        if project:
            note.project_id = project.id
    
    # Aktualizuj pozostałe pola
    if 'content' in data:
        note.content = data['content']
    if 'category' in data:
        note.category = data['category']
    
    note.updated_at = datetime.utcnow()
    
    try:
        db.session.commit()
        return jsonify({
            'id': note.id,
            'content': note.content,
            'category': note.category,
            'project_tag': note.project.tag,
            'updated_at': note.updated_at.isoformat()
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# Endpoint do pobierania pojedynczego zadania
@app.route('/api/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    task = Task.query.get_or_404(task_id)
    return jsonify({
        'id': task.id,
        'content': task.content,
        'category': task.category,
        'priority': task.priority,
        'deadline': task.deadline.isoformat() if task.deadline else None,
        'project_tag': task.project.tag,
        'created_at': task.created_at.isoformat(),
        'updated_at': task.updated_at.isoformat(),
        'subtasks': [{
            'id': s.id,
            'content': s.content,
            'is_completed': s.is_completed
        } for s in task.subtasks]
    })

# Endpoint do aktualizacji zadania
@app.route('/api/tasks/<int:task_id>', methods=['PUT'])
def update_task(task_id):
    task = Task.query.get_or_404(task_id)
    data = request.json
    
    # Znajdź projekt po tagu lub zostaw obecny
    if 'project_tag' in data:
        project = Project.query.filter_by(tag=data['project_tag']).first()
        if project:
            task.project_id = project.id
    
    # Aktualizuj pozostałe pola
    if 'content' in data:
        task.content = data['content']
    if 'category' in data:
        task.category = data['category']
    if 'priority' in data:
        task.priority = data['priority']
    if 'deadline' in data:
        try:
            task.deadline = datetime.fromisoformat(data['deadline']) if data['deadline'] else None
        except ValueError:
            return jsonify({'error': 'Invalid deadline format'}), 400
    
    task.updated_at = datetime.utcnow()
    
    try:
        db.session.commit()
        return jsonify({
            'id': task.id,
            'content': task.content,
            'category': task.category,
            'priority': task.priority,
            'deadline': task.deadline.isoformat() if task.deadline else None,
            'project_tag': task.project.tag,
            'updated_at': task.updated_at.isoformat()
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# Tworzenie bazy danych i domyślnego projektu #inbox
def init_db():
    with app.app_context():
        db.create_all()
        # Sprawdź czy #inbox już istnieje
        inbox = Project.query.filter_by(tag='#inbox').first()
        if not inbox:
            inbox = Project(tag='#inbox', name='Inbox')
            db.session.add(inbox)
            db.session.commit()

# Dodaj walidację danych
def validate_content(content):
    if not content or len(content.strip()) == 0:
        raise ValueError("Content cannot be empty")
    if len(content) > 5000:  # Limit na długość tekstu
        raise ValueError("Content too long (max 5000 characters)")
    return content.strip()

def sanitize_input(text):
    # Usuń niebezpieczne tagi HTML
    return bleach.clean(text, tags=[], strip=True)

# Nowe endpointy dla czatu
@app.route('/api/chat', methods=['POST'])
@limiter.limit("50 per minute")  # Limit requestów
def chat():
    data = request.json
    message = data.get('message')
    session_id = data.get('session_id')
    
    if not message or not session_id:
        return jsonify({'error': 'Missing message or session_id'}), 400
    
    try:
        response = chat_manager.generate_response(message, session_id)
        return jsonify({
            'response': response,
            'history': chat_manager.get_chat_history(session_id)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat/history', methods=['GET'])
def get_chat_history():
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'Missing session_id'}), 400
    
    history = chat_manager.get_chat_history(session_id)
    return jsonify({'history': history})

@app.route('/api/chat/clear', methods=['POST'])
def clear_chat():
    session_id = request.json.get('session_id')
    if not session_id:
        return jsonify({'error': 'Missing session_id'}), 400
    
    chat_manager.clear_chat_history(session_id)
    return jsonify({'message': 'Chat history cleared'})

@app.route('/api/chat/test', methods=['GET'])
def test_chat():
    try:
        if chat_manager.model is None:
            chat_manager.initialize_model()
        response = chat_manager.generate_response(
            "Hello, how are you?",
            "test_session"
        )
        return jsonify({
            'status': 'success',
            'response': response
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

@app.route('/api/chat/create', methods=['POST'])
def create_from_chat():
    data = request.json
    message = data.get('message')
    session_id = data.get('session_id')
    content_type = data.get('type', 'note')  # 'note' lub 'task'
    
    if not message or not session_id:
        return jsonify({'error': 'Missing message or session_id'}), 400
    
    try:
        # Przygotuj kontekst
        context = {
            'type': content_type,
            'content': message,
            'project': data.get('project', '#inbox'),
            'category': data.get('category', ''),
        }
        
        if content_type == 'task':
            context.update({
                'priority': data.get('priority', 'medium'),
                'deadline': data.get('deadline', '')
            })
        
        # Generuj odpowiedź z kontekstem
        response = chat_manager.generate_response(message, session_id, context)
        
        return jsonify({
            'response': response,
            'context': context
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Nowe endpointy dla zarządzania modelami
@app.route('/api/chat/models', methods=['GET'])
def get_available_models():
    """Zwraca listę dostępnych modeli"""
    return jsonify(chat_manager.llm.get_available_models())

@app.route('/api/chat/model', methods=['PUT'])
def switch_model():
    """Przełącza aktualny model"""
    data = request.json
    model_type = data.get('model_type')
    
    if not model_type:
        return jsonify({'error': 'Missing model_type parameter'}), 400
        
    if chat_manager.llm.switch_model(model_type):
        return jsonify({
            'message': f'Successfully switched to {model_type} model',
            'model_info': chat_manager.llm.MODELS[model_type]
        })
    else:
        return jsonify({'error': 'Invalid model type'}), 400

# Endpoint to save AI settings
@app.route('/api/chat/settings', methods=['POST'])
def save_chat_settings():
    """Save AI settings.

    Expected JSON fields:
    - temperature: float between 0 and 2 (controls randomness)
    - maxTokens: integer between 1 and 4096 (max response length)
    - apiKey: string, API key for authentication
    - userIdentity: string representing the user's identity
    - shortTermPlans: string (multi-line) for short term plans
    - longTermPlans: string (multi-line) for long term plans
    - responseTone: string (e.g., 'Neutral', 'Friendly', 'Professional', 'Casual')
    - responseLength: string (e.g., 'Short', 'Medium', 'Long')
    - llmSubjectArea: string describing the subject area
    """
    data = request.json
    try:
        temperature = float(data.get('temperature', 0.7))
        max_tokens = int(data.get('maxTokens', 512))
        
        if not 0 <= temperature <= 2:
            return jsonify({'error': 'Temperature must be between 0 and 2'}), 400
        if not 1 <= max_tokens <= 4096:
            return jsonify({'error': 'Max tokens must be between 1 and 4096'}), 400
        
        # Update settings in chat manager
        chat_manager.llm.temperature = temperature
        chat_manager.llm.max_tokens = max_tokens
        
        chat_manager.user_identity = data.get('userIdentity', '')
        chat_manager.short_term_plans = data.get('shortTermPlans', '')
        chat_manager.long_term_plans = data.get('longTermPlans', '')
        chat_manager.response_tone = data.get('responseTone', '')
        chat_manager.response_length = data.get('responseLength', '')
        chat_manager.llm_subject_area = data.get('llmSubjectArea', '')
        
        return jsonify({
            'message': 'Settings saved successfully',
            'settings': {
                'temperature': temperature,
                'maxTokens': max_tokens,
                'userIdentity': chat_manager.user_identity,
                'shortTermPlans': chat_manager.short_term_plans,
                'longTermPlans': chat_manager.long_term_plans,
                'responseTone': chat_manager.response_tone,
                'responseLength': chat_manager.response_length,
                'llmSubjectArea': chat_manager.llm_subject_area

            }
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to save settings: {str(e)}'}), 500

# Dodaj inicjalizację chat managera przy starcie aplikacji
def create_app():
    with app.app_context():
        # Inicjalizacja bazy danych
        db.create_all()
        
        # Sprawdź czy #inbox już istnieje
        inbox = Project.query.filter_by(tag='#inbox').first()
        if not inbox:
            print("Creating default #inbox project...")
            inbox = Project(tag='#inbox', name='Inbox')
            db.session.add(inbox)
            try:
                db.session.commit()
                print("Default #inbox project created successfully")
            except Exception as e:
                db.session.rollback()
                print(f"Error creating default #inbox project: {str(e)}")
        
        # Removed chat_manager.initialize_model() call since it's not needed
    return app

if __name__ == '__main__':
    create_app()
    app.run(debug=True)