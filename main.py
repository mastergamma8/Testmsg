from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import or_
from datetime import datetime, timedelta
import os

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret_key_change_me')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///messenger.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")  # allow local testing

online_users = {}  # username -> sid

# --- Models ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    last_seen = db.Column(db.DateTime)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(50))
    receiver = db.Column(db.String(50))
    text = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)
    replied_to = db.Column(db.Integer, nullable=True)  # message id

with app.app_context():
    db.create_all()

# --- Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/check_session', methods=['GET'])
def check_session():
    if 'username' in session:
        return jsonify({'status': 'logged_in', 'username': session['username']})
    return jsonify({'status': 'guest'})

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({'status':'error','message':'Поля обязательны'})
    if User.query.filter_by(username=username).first():
        return jsonify({'status':'error','message':'Имя занято!'})
    hashed_pw = generate_password_hash(password)
    new_user = User(username=username, password_hash=hashed_pw, last_seen=datetime.utcnow())
    db.session.add(new_user)
    db.session.commit()
    session.permanent = True
    session['username'] = username
    return jsonify({'status':'success','username':username})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({'status':'error','message':'Поля обязательны'})
    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(user.password_hash, password):
        session.permanent = True
        session['username'] = username
        return jsonify({'status':'success','username':username})
    return jsonify({'status':'error','message':'Ошибка входа'})

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    return jsonify({'status':'success'})

@app.route('/search_user', methods=['POST'])
def search_user():
    query = (request.json or {}).get('query', '')
    if not query:
        return jsonify([])
    users = User.query.filter(User.username.contains(query)).limit(10).all()
    results = [u.username for u in users if u.username != session.get('username')]
    return jsonify(results)

@app.route('/get_chats', methods=['GET'])
def get_chats():
    me = session.get('username')
    if not me: return jsonify([])
    messages = Message.query.filter(or_(Message.sender==me, Message.receiver==me)).all()
    partners_names = set()
    for m in messages:
        partners_names.add(m.receiver if m.sender == me else m.sender)
    chat_list = []
    for name in partners_names:
        user_obj = User.query.filter_by(username=name).first()
        is_online = name in online_users
        unread_count = Message.query.filter_by(sender=name, receiver=me, is_read=False).count()
        last_seen_iso = user_obj.last_seen.isoformat() + "Z" if user_obj and user_obj.last_seen else None
        chat_list.append({'username': name, 'online': is_online, 'unread': unread_count, 'last_seen': last_seen_iso})
    # sort by unread and existence (optional)
    chat_list.sort(key=lambda x: (-x['unread'], x['username']))
    return jsonify(chat_list)

@app.route('/get_history', methods=['POST'])
def get_history():
    payload = request.json or {}
    partner = payload.get('partner')
    me = session.get('username')
    if not me or not partner:
        return jsonify({'messages': [], 'partner_status': {'online': False, 'last_seen': None}})
    # mark unread -> read
    unread_msgs = Message.query.filter_by(sender=partner, receiver=me, is_read=False).all()
    for msg in unread_msgs:
        msg.is_read = True
    db.session.commit()
    # notify partner that messages read (realtime)
    if unread_msgs and partner in online_users:
        socketio.emit('messages_read', {'reader': me}, room=partner)

    msgs = Message.query.filter(
        ((Message.sender == me) & (Message.receiver == partner)) |
        ((Message.sender == partner) & (Message.receiver == me))
    ).order_by(Message.timestamp.asc()).all()

    # prepare messages with replied_text if any
    out_msgs = []
    for m in msgs:
        replied_text = None
        replied_sender = None
        if m.replied_to:
            orig = Message.query.filter_by(id=m.replied_to).first()
            if orig:
                replied_text = orig.text
                replied_sender = orig.sender
        out_msgs.append({'id': m.id, 'sender': m.sender, 'text': m.text, 'is_read': m.is_read, 'replied_to': m.replied_to, 'replied_text': replied_text, 'replied_sender': replied_sender})
    partner_obj = User.query.filter_by(username=partner).first()
    is_online = partner in online_users
    last_seen_iso = partner_obj.last_seen.isoformat() + "Z" if partner_obj and partner_obj.last_seen else None
    return jsonify({'messages': out_msgs, 'partner_status': {'online': is_online, 'last_seen': last_seen_iso}})

# --- SocketIO handlers ---
@socketio.on('connect')
def on_connect():
    pass

@socketio.on('disconnect')
def on_disconnect():
    disconnected_user = None
    for username, sid in list(online_users.items()):
        if sid == request.sid:
            disconnected_user = username
            del online_users[username]
            break
    if disconnected_user:
        user = User.query.filter_by(username=disconnected_user).first()
        if user:
            user.last_seen = datetime.utcnow()
            db.session.commit()
            emit('user_status_change', {'username': disconnected_user, 'status': 'offline', 'last_seen': user.last_seen.isoformat() + "Z"}, broadcast=True)

@socketio.on('join')
def on_join(data):
    username = data.get('username')
    if not username: return
    session['username'] = username
    join_room(username)
    online_users[username] = request.sid
    emit('user_status_change', {'username': username, 'status': 'online'}, broadcast=True)

@socketio.on('send_message')
def handle_message(data):
    sender = data.get('sender')
    receiver = data.get('receiver')
    text = data.get('text', '')
    replied_to = data.get('replied_to', None)
    if not sender or not receiver or not text:
        return
    msg = Message(sender=sender, receiver=receiver, text=text, is_read=False, replied_to=replied_to)
    db.session.add(msg)
    db.session.commit()

    # prepare reply metadata to include in emit
    replied_text = None
    replied_sender = None
    if replied_to:
        orig = Message.query.filter_by(id=replied_to).first()
        if orig:
            replied_text = orig.text
            replied_sender = orig.sender

    payload = {
        'id': msg.id,
        'sender': sender,
        'receiver': receiver,
        'text': text,
        'is_read': False,
        'timestamp': msg.timestamp.isoformat() + "Z",
        'replied_to': replied_to,
        'replied_text': replied_text,
        'replied_sender': replied_sender
    }
    # send to receiver and sender (so both clients update)
    emit('new_message', payload, room=receiver)
    emit('new_message', payload, room=sender)
    emit('update_chat_list', {'partner': sender}, room=receiver)

@socketio.on('typing')
def on_typing(data):
    emit('display_typing', {'sender': data.get('sender')}, room=data.get('receiver'))

@socketio.on('stop_typing')
def on_stop_typing(data):
    emit('hide_typing', {'sender': data.get('sender')}, room=data.get('receiver'))

@socketio.on('mark_read_realtime')
def on_mark_read(data):
    sender = data.get('sender')
    me = data.get('reader')
    msgs = Message.query.filter_by(sender=sender, receiver=me, is_read=False).all()
    for m in msgs:
        m.is_read = True
    db.session.commit()
    emit('messages_read', {'reader': me}, room=sender)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
