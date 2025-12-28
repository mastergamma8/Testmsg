import eventlet
eventlet.monkey_patch()

bind = "0.0.0.0:8080"
workers = 1
worker_class = "eventlet"
timeout = 120
keepalive = 5
