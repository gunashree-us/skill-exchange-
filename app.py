from core import DEBUG_MODE, app, init_db, socketio

# Initialize database automatically on startup
init_db()

# Import route modules for side effects so decorators and Socket.IO handlers register once.
import routes.admin  # noqa: F401
import routes.auth  # noqa: F401
import routes.chat  # noqa: F401
import routes.main  # noqa: F401
import routes.notifications  # noqa: F401
import routes.profile  # noqa: F401
import routes.requests  # noqa: F401
import routes.skills  # noqa: F401


if __name__ == "__main__":
    # Start the Flask dev server; use init_db.py for explicit schema/bootstrap runs.
    socketio.run(app, host="0.0.0.0", port=5000, debug=DEBUG_MODE)
