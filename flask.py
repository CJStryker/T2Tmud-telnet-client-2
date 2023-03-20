from flask import Flask, render_template
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
db = SQLAlchemy(app)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def on_connect():
    print('Client connected.')

@socketio.on('disconnect')
def on_disconnect():
    print('Client disconnected.')

@app.route('/about')
def about():
    return render_template('about.html')

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
  
    def __init__(self, username, email):
        self.username = username
        self.email = email
      
    def __repr__(self):
        return '<User %r>' % self.username

@socketio.on('user_login')
def on_user_login(username, email):
  user = User.query.filter_by(username=username).first()
  if user is None:
    user = User(username, email)
    db.session.add(user)
    db.session.commit()
    print('User %s added.' % username)
  else:
    print('User %s already exists.' % username)
    
@socketio.on('user_logout')
def on_user_logout(username):
  user = User.query.filter_by(username=username).first()
  if user is not None:
    db.session.delete(user)
    db.session.commit()
    print('User %s removed.' % username)
    
@socketio.on('user_update')
def on_user_update(username, email):
  user = User.query.filter_by(username=username).first()
  if user is not None:
    user.email = email
    db.session.commit()
    print('User %s updated.' % username)
    
@socketio.on('user_add')
def on_user_add(username, email):
  user = User(username, email)
  db.session.add(user)
  db.session.commit()
  print('User %s added.' % username)
  
@socketio.on('user_delete')
def on_user_delete(username):
  user = User.query.filter_by(username=username).first()
  if user is not None:

          
    
print(id, username, email)
    def __repr__(self):
        return f'<User {self.username}>'

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=3333)