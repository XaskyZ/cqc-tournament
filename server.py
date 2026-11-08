import os
from config.wsgi import application
from waitress import serve

if __name__ == '__main__':
    host = os.environ.get('LISTEN_HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8000'))
    print(f'CQC Tournament: http://{host}:{port}', flush=True)
    serve(application, host=host, port=port, threads=4)
