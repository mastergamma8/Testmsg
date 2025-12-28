from gevent import monkey
monkey.patch_all()

bind = "0.0.0.0:8080"
workers = 1
worker_class = "gevent"  # <--- БЫЛО "eventlet", СТАЛО "gevent"
timeout = 120
keepalive = 5
