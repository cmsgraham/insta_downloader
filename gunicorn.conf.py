import multiprocessing

# Single worker with threads — required for in-memory per-user state
workers = 1
threads = 4
worker_class = "gthread"
bind = "0.0.0.0:5000"
timeout = 120
accesslog = "-"
errorlog = "-"
control_socket_disable = True
