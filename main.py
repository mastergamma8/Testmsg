from flask import Flask, render_template, request, jsonify, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import or_
from datetime import datetime, timedelta, timezone
import os
import re
import uuid
import user_agents # Для определения устройства (нужно установить: pip install user-agents)
# Если user_agents нет, можно использовать request.headers.get('User-Agent') напрямую

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'secret_key_change_me_v12_pro_sessions' 
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///messenger.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)
app.config['UPLOAD_FOLDER'] = 'static/avatars'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Храним активные сокеты: {username: set(sid1, sid2, ...)}
online_users = {}

# --- МОДЕЛИ ДАННЫХ ---

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    display_name = db.Column(db.String(50), nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    avatar = db.Column(db.String(200), nullable=True) 
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связь с сессиями
    sessions = db.relationship('UserSession', backref='user', lazy='dynamic', cascade="all, delete-orphan")

class UserSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_token = db.Column(db.String(36), unique=True, nullable=False) # UUID токен
    device_info = db.Column(db.String(200)) # Например: "Chrome on Windows"
    ip_address = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_active = db.Column(db.DateTime, default=datetime.utcnow)

class Reaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False)
    user_username = db.Column(db.String(50), nullable=False)
    emoji = db.Column(db.String(10), nullable=False)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(50))
    receiver = db.Column(db.String(50))
    text = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)
    is_edited = db.Column(db.Boolean, default=False)

    reply_to_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=True)
    replies = db.relationship('Message', backref=db.backref('parent', remote_side=[id]), lazy='dynamic')
    reactions = db.relationship('Reaction', backref='message', lazy='dynamic', cascade="all, delete-orphan")

    def to_dict(self):
        sender_user = User.query.filter_by(username=self.sender).first()
        sender_display = sender_user.display_name if sender_user else self.sender

        reply_data = None
        if self.reply_to_id:
            parent = Message.query.get(self.reply_to_id)
            if parent:
                parent_user = User.query.filter_by(username=parent.sender).first()
                parent_display = parent_user.display_name if parent_user else parent.sender
                reply_data = {'sender': parent_display, 'text': parent.text}
            else:
                reply_data = {'sender': 'Сообщение', 'text': 'удалено'}

        reactions_data = {}
        for r in self.reactions.all():
            if r.emoji not in reactions_data:
                reactions_data[r.emoji] = []
            reactions_data[r.emoji].append(r.user_username)

        return {
            'id': self.id,
            'sender_username': self.sender,
            'sender_display': sender_display,
            'receiver': self.receiver,
            'text': self.text,
            'timestamp': self.timestamp.replace(tzinfo=timezone.utc).isoformat() if self.timestamp else None,
            'is_read': self.is_read,
            'is_edited': self.is_edited,
            'reply_to': reply_data,
            'reactions': reactions_data
        }

with app.app_context():
    db.create_all()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def clean_username(u):
    if not u: return ""
    return u.replace('@', '').strip().lower()

def validate_username_format(u):
    if not u: return False, "Пустой юзернейм"
    if len(u) < 5: return False, "Минимум 5 символов"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9]*$", u):
        return False, "Только латиница и цифры, первый символ - буква"
    return True, ""

def get_avatar_url(filename):
    if filename:
        return url_for('static', filename=f'avatars/{filename}')
    return None

def parse_user_agent(ua_string):
    # Простой парсер для отображения устройства
    try:
        from user_agents import parse
        user_agent = parse(ua_string)
        return str(user_agent)
    except ImportError:
        return ua_string[:50] # Fallback, если библиотека не установлена

# --- МАРШРУТЫ (ROUTES) ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/check_session', methods=['GET'])
def check_session_route():
    # Проверяем и cookie, и валидность токена в БД
    if 'username' in session and 'token' in session:
        user = User.query.filter_by(username=session['username']).first()
        if user:
            # Проверяем, существует ли эта сессия (не удалили ли ее удаленно)
            user_session = UserSession.query.filter_by(session_token=session['token']).first()
            if user_session:
                user_session.last_active = datetime.utcnow()
                db.session.commit()
                return jsonify({
                    'status': 'logged_in', 
                    'username': user.username, 
                    'display_name': user.display_name,
                    'avatar_url': get_avatar_url(user.avatar)
                })
            else:
                # Сессия недействительна (удалена)
                session.clear()
    
    return jsonify({'status': 'guest'})

@app.route('/check_username_availability', methods=['POST'])
def check_availability():
    req_username = clean_username(request.json.get('username'))
    current_session_user = session.get('username')

    if current_session_user and req_username == current_session_user:
        return jsonify({'available': True})

    is_valid, msg = validate_username_format(req_username)
    if not is_valid:
        return jsonify({'available': False, 'message': msg})

    existing = User.query.filter_by(username=req_username).first()
    if existing:
        return jsonify({'available': False, 'message': 'Занят'})

    return jsonify({'available': True})

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    raw_username = clean_username(data.get('username'))
    display_name = data.get('display_name', '').strip()
    password = data.get('password')
    ua_string = request.headers.get('User-Agent')

    if not raw_username or not password or not display_name:
        return jsonify({'status': 'error', 'message': 'Заполните все поля'})
    
    is_valid, msg = validate_username_format(raw_username)
    if not is_valid:
        return jsonify({'status': 'error', 'message': msg})

    if User.query.filter_by(username=raw_username).first():
        return jsonify({'status': 'error', 'message': 'Юзернейм уже занят!'})

    hashed_pw = generate_password_hash(password)
    new_user = User(username=raw_username, display_name=display_name, password_hash=hashed_pw, last_seen=datetime.utcnow())
    db.session.add(new_user)
    db.session.commit() # Получаем ID

    # Создаем сессию
    token = str(uuid.uuid4())
    new_session = UserSession(
        user_id=new_user.id,
        session_token=token,
        device_info=parse_user_agent(ua_string),
        ip_address=request.remote_addr
    )
    db.session.add(new_session)
    db.session.commit()

    session.permanent = True
    session['username'] = raw_username
    session['token'] = token
    
    return jsonify({'status': 'success', 'username': raw_username, 'display_name': display_name})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    raw_username = clean_username(data.get('username'))
    password = data.get('password')
    ua_string = request.headers.get('User-Agent')

    user = User.query.filter_by(username=raw_username).first()

    if user and check_password_hash(user.password_hash, password):
        # Создаем новую запись сессии (устройства)
        token = str(uuid.uuid4())
        new_session = UserSession(
            user_id=user.id,
            session_token=token,
            device_info=parse_user_agent(ua_string),
            ip_address=request.remote_addr
        )
        db.session.add(new_session)
        db.session.commit()

        session.permanent = True
        session['username'] = user.username
        session['token'] = token
        
        return jsonify({'status': 'success', 'username': user.username, 'display_name': user.display_name})
    return jsonify({'status': 'error', 'message': 'Неверный логин или пароль'})

@app.route('/logout', methods=['POST'])
def logout():
    # Удаляем текущую сессию из БД
    if 'token' in session:
        UserSession.query.filter_by(session_token=session['token']).delete()
        db.session.commit()
    
    session.clear()
    return jsonify({'status': 'success'})

@app.route('/get_sessions', methods=['GET'])
def get_sessions():
    if 'username' not in session: return jsonify({'error': 'Auth required'}), 401
    
    user = User.query.filter_by(username=session['username']).first()
    if not user: return jsonify({'error': 'User not found'}), 404

    sessions = UserSession.query.filter_by(user_id=user.id).order_by(UserSession.last_active.desc()).all()
    result = []
    current_token = session.get('token')

    for s in sessions:
        result.append({
            'id': s.id,
            'device_info': s.device_info,
            'ip': s.ip_address,
            'last_active': s.last_active.replace(tzinfo=timezone.utc).isoformat(),
            'is_current': (s.session_token == current_token)
        })
    return jsonify(result)

@app.route('/terminate_session', methods=['POST'])
def terminate_session():
    if 'username' not in session: return jsonify({'error': 'Auth required'}), 401
    
    data = request.json
    session_id = data.get('session_id')
    user = User.query.filter_by(username=session['username']).first()
    
    # Удаляем сессию только если она принадлежит этому пользователю
    sess_to_del = UserSession.query.filter_by(id=session_id, user_id=user.id).first()
    if sess_to_del:
        db.session.delete(sess_to_del)
        db.session.commit()
        return jsonify({'status': 'success'})
    
    return jsonify({'status': 'error', 'message': 'Session not found'})

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': 'Не авторизован'})

    display_name = request.form.get('display_name')
    new_username = clean_username(request.form.get('username'))
    new_password = request.form.get('new_password')
    old_password = request.form.get('old_password')

    user = User.query.filter_by(username=session['username']).first()
    if not user: return jsonify({'status': 'error', 'message': 'User not found'})

    if new_password and len(new_password) > 0:
        if not old_password or not check_password_hash(user.password_hash, old_password):
            return jsonify({'status': 'error', 'message': 'Неверный текущий пароль'})
        user.password_hash = generate_password_hash(new_password)

    if display_name:
        user.display_name = display_name.strip()

    if new_username and new_username != user.username:
        is_valid, msg = validate_username_format(new_username)
        if not is_valid:
             return jsonify({'status': 'error', 'message': msg})

        if User.query.filter_by(username=new_username).first():
            return jsonify({'status': 'error', 'message': 'Юзернейм занят'})
        
        old_handle = user.username
        user.username = new_username
        # Обновляем ссылки в сообщениях
        Message.query.filter_by(sender=old_handle).update({'sender': new_username})
        Message.query.filter_by(receiver=old_handle).update({'receiver': new_username})
        Reaction.query.filter_by(user_username=old_handle).update({'user_username': new_username})
        session['username'] = new_username

    if 'avatar' in request.files:
        file = request.files['avatar']
        if file and file.filename != '':
            new_filename = f"user_{user.id}_{int(datetime.utcnow().timestamp())}.jpg"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
            
            if user.avatar:
                try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'], user.avatar))
                except: pass
            user.avatar = new_filename

    try:
        db.session.commit()
        socketio.emit('user_profile_updated', {
            'username': user.username,
            'display_name': user.display_name,
            'avatar_url': get_avatar_url(user.avatar)
        })

        return jsonify({
            'status': 'success', 
            'username': user.username,
            'display_name': user.display_name,
            'avatar_url': get_avatar_url(user.avatar)
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/search_user', methods=['POST'])
def search_user():
    query = request.json.get('query', '').lower().strip().replace('@', '')
    if not query: return jsonify([])

    users = User.query.filter(
        or_(
            User.username.contains(query),
            User.display_name.contains(query)
        )
    ).limit(10).all()

    results = []
    me = session.get('username')

    for u in users:
        if u.username != me:
            results.append({
                'username': u.username,
                'display_name': u.display_name
            })

    return jsonify(results)

@app.route('/get_chats', methods=['GET'])
def get_chats():
    me = session.get('username')
    if not me: return jsonify([])

    messages = Message.query.filter(or_(Message.sender == me, Message.receiver == me)).order_by(Message.timestamp.desc()).all()
    partners_map = {}

    for m in messages:
        partner_username = m.receiver if m.sender == me else m.sender
        if partner_username not in partners_map:
            partners_map[partner_username] = { 'last_msg': m, 'unread': 0 }
        if m.receiver == me and not m.is_read:
            partners_map[partner_username]['unread'] += 1

    partner_usernames = list(partners_map.keys())
    if not partner_usernames:
        return jsonify([])
        
    users_obj = User.query.filter(User.username.in_(partner_usernames)).all()
    users_dict = {u.username: u for u in users_obj}

    chat_list = []
    for username, data in partners_map.items():
        user_obj = users_dict.get(username)
        if not user_obj: continue 

        is_online = username in online_users and len(online_users[username]) > 0
        last_seen_iso = user_obj.last_seen.replace(tzinfo=timezone.utc).isoformat() if user_obj.last_seen else None

        chat_list.append({
            'username': username, 
            'display_name': user_obj.display_name, 
            'avatar_url': get_avatar_url(user_obj.avatar),
            'online': is_online,
            'unread': data['unread'],
            'last_seen': last_seen_iso,
            'preview': (data['last_msg'].text[:30] + '...') if len(data['last_msg'].text) > 30 else data['last_msg'].text,
            'timestamp': data['last_msg'].timestamp.replace(tzinfo=timezone.utc).isoformat()
        })
    
    chat_list.sort(key=lambda x: x['timestamp'], reverse=True)

    return jsonify(chat_list)

@app.route('/get_history', methods=['POST'])
def get_history():
    partner_username = request.json.get('partner')
    me = session.get('username')
    if not me or not partner_username: return jsonify({'error': 'No auth'})

    Message.query.filter_by(sender=partner_username, receiver=me, is_read=False).update({'is_read': True})
    db.session.commit()

    # Отправляем уведомление о прочтении (если партнер онлайн)
    # Используем broadcast через комнату имени пользователя
    socketio.emit('messages_read', {'reader': me}, room=partner_username)

    msgs = Message.query.filter(
        ((Message.sender == me) & (Message.receiver == partner_username)) |
        ((Message.sender == partner_username) & (Message.receiver == me))
    ).order_by(Message.timestamp.asc()).all()

    partner_obj = User.query.filter_by(username=partner_username).first()
    is_online = partner_username in online_users and len(online_users[partner_username]) > 0
    last_seen_iso = partner_obj.last_seen.replace(tzinfo=timezone.utc).isoformat() if partner_obj and partner_obj.last_seen else None

    return jsonify({
        'messages': [m.to_dict() for m in msgs],
        'partner_status': {
            'online': is_online, 
            'last_seen': last_seen_iso,
            'display_name': partner_obj.display_name if partner_obj else partner_username
        }
    })

# --- SOCKET EVENTS ---

@socketio.on('join')
def on_join(data):
    username = data['username']
    session['username'] = username
    join_room(username) # Добавляем ЭТОТ сокет в комнату пользователя
    
    if username not in online_users:
        online_users[username] = set()
    online_users[username].add(request.sid)
    
    # Уведомляем всех, что пользователь теперь онлайн
    emit('user_status_change', {'username': username, 'status': 'online'}, broadcast=True)

@socketio.on('disconnect')
def on_disconnect():
    username = session.get('username')
    if username and username in online_users:
        if request.sid in online_users[username]:
            online_users[username].remove(request.sid)
        
        # Если у пользователя не осталось активных соединений (устройств)
        if len(online_users[username]) == 0:
            del online_users[username]
            user = User.query.filter_by(username=username).first()
            if user:
                user.last_seen = datetime.utcnow()
                db.session.commit()
                emit('user_status_change', {
                    'username': username, 
                    'status': 'offline', 
                    'last_seen': user.last_seen.replace(tzinfo=timezone.utc).isoformat()
                }, broadcast=True)

@socketio.on('send_message')
def handle_message(data):
    sender = data['sender']
    receiver = data['receiver']
    text = data['text']
    reply_to_id = data.get('reply_to_id') 

    msg = Message(sender=sender, receiver=receiver, text=text, is_read=False, reply_to_id=reply_to_id, timestamp=datetime.utcnow())
    db.session.add(msg)
    db.session.commit()

    msg_data = msg.to_dict()
    # Отправляем в комнаты (доставится на все устройства пользователей)
    emit('new_message', msg_data, room=receiver)
    emit('new_message', msg_data, room=sender)

    sender_obj = User.query.filter_by(username=sender).first()
    if sender_obj:
        emit('incoming_chat_update', {
            'username': sender, 
            'display_name': sender_obj.display_name,
            'avatar_url': get_avatar_url(sender_obj.avatar),
            'text': text,
            'timestamp': msg_data['timestamp']
        }, room=receiver)

@socketio.on('edit_message')
def handle_edit_message(data):
    msg_id = data.get('message_id')
    new_text = data.get('new_text')
    sender = data.get('sender')
    partner = data.get('partner')

    msg = Message.query.get(msg_id)
    if msg and msg.sender == sender:
        msg.text = new_text
        msg.is_edited = True
        db.session.commit()

        update_data = {'id': msg_id, 'text': new_text}
        emit('message_updated', update_data, room=sender)
        emit('message_updated', update_data, room=partner)

@socketio.on('delete_message')
def handle_delete_message(data):
    msg_id = data.get('message_id')
    sender = data.get('sender')

    msg = Message.query.get(msg_id)
    if msg and msg.sender == sender:
        receiver = msg.receiver
        db.session.delete(msg)
        db.session.commit()
        emit('message_removed', {'id': msg_id}, room=sender)
        emit('message_removed', {'id': msg_id}, room=receiver)

@socketio.on('reaction_update')
def handle_reaction_update(data):
    msg_id = data.get('message_id')
    emoji = data.get('emoji')
    sender = data.get('sender')
    partner = data.get('partner') 

    # 1. Удаляем ЛЮБУЮ предыдущую реакцию этого пользователя на это сообщение
    Reaction.query.filter_by(message_id=msg_id, user_username=sender).delete()
    
    # 2. Добавляем новую реакцию (если бы это был toggle, мы бы проверили существование, но мы хотим заменить)
    # Если нужно, чтобы повторное нажатие той же эмодзи убирало её, можно добавить проверку.
    # Но по условию "одна реакция на сообщение", мы просто ставим новую.
    # Если вы хотите "toggle" (нажал лайк - поставил, нажал еще раз - убрал), раскомментируйте код ниже, 
    # но тогда нужно знать, какая была прошлая реакция. 
    # Сейчас реализация: всегда ставим новую, удаляя старую.
    
    new_reaction = Reaction(message_id=msg_id, user_username=sender, emoji=emoji)
    db.session.add(new_reaction)

    db.session.commit()

    msg = Message.query.get(msg_id)
    reactions_data = {}
    if msg:
        for r in msg.reactions.all():
            if r.emoji not in reactions_data:
                reactions_data[r.emoji] = []
            reactions_data[r.emoji].append(r.user_username)

    emit('reaction_updated', {'message_id': msg_id, 'reactions': reactions_data}, room=sender)
    emit('reaction_updated', {'message_id': msg_id, 'reactions': reactions_data}, room=partner)

@socketio.on('typing')
def on_typing(data): 
    emit('display_typing', {'sender': data['sender']}, room=data['receiver'])

@socketio.on('stop_typing')
def on_stop_typing(data): 
    emit('hide_typing', {'sender': data['sender']}, room=data['receiver'])

@socketio.on('mark_read_realtime')
def on_mark_read(data):
    sender = data['sender']
    me = data['reader']
    Message.query.filter_by(sender=sender, receiver=me, is_read=False).update({'is_read': True})
    db.session.commit()
    emit('messages_read', {'reader': me}, room=sender)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
