import os
import logging
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session
from jinja2 import TemplateNotFound
import psycopg2

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'dev-secret')

log.debug("Starting server.py - attempting DB connect and app setup")

# Try to connect to DB with error handling so the server can start even if DB is down
db_connect_error = None
conn = None
cur = None
try:
    conn = psycopg2.connect(
        dbname='Registration',
        user='postgres',
        password='1516',
        host='localhost',
        port='5432'
    )
    cur = conn.cursor()
    log.info("Connected to Postgres successfully")
except Exception as e:
    db_connect_error = str(e)
    conn = None
    cur = None
    log.warning("Postgres connect failed: %s", db_connect_error)

# Ensure the user_id sequence exists and is linked to the registration table
if cur is not None:
    try:
        cur.execute("""
            CREATE SEQUENCE IF NOT EXISTS registration_user_id_seq;
            ALTER TABLE registration ALTER COLUMN user_id SET DEFAULT nextval('registration_user_id_seq');
            ALTER SEQUENCE registration_user_id_seq OWNED BY registration.user_id;
            SELECT setval('registration_user_id_seq', COALESCE((SELECT MAX(user_id) FROM registration), 0) );
        """)
        conn.commit()
        log.info("Ensured registration_user_id_seq sequence is set up")
    except Exception as e:
        log.warning("Failed to set up user_id sequence: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                log.exception("Rollback failed during sequence setup")
else:
    log.warning("Skipping registration_user_id_seq setup because DB connection is not available: %s", db_connect_error)

from flask import request, redirect, url_for, render_template_string, session
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Note: this fallback does not verify credentials.
        session['user'] = request.form.get('username')
        return redirect(url_for('home_loggedin'))
    return render_template_string(
        '<h2>Login (fallback)</h2>'
        '<form method="post">'
        '<input name="username" placeholder="username">'
        '<button type="submit">Login</button>'
        '</form>'
    )

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('index'))

@app.route('/')
def index():
    try:
        # preferred: templates/student_form.html
        return render_template('student_form.html')
    except TemplateNotFound:
        # fallback to the file you have in the project root named "student form.html"
        return send_from_directory(os.path.dirname(__file__), 'student form.html')
    except Exception as e:
        log.exception("Error rendering form")
        return f"<h2>Error rendering form:</h2><pre>{e}</pre>", 500

@app.route('/submit', methods=['POST'])
def submit():
    data = request.form
    log.debug("Form Data Received: %s", dict(data))

    if cur is None or conn is None:
        log.error("DB not connected, cannot save form: %s", db_connect_error)
        return f"<h2>DB not connected:</h2><pre>{db_connect_error}</pre><a href='/'>Go Back</a>", 500

    # collect form values (support multiple common names)
    full_name = data.get('full_name') or data.get('name') or data.get('fullname') or data.get('fullName')
    raw_user_id = data.get('user_id')
    username = data.get('username') or data.get('user')
    password_hash = data.get('password_hash') or data.get('password')
    email = data.get('email')
    phone = data.get('phone')
    father_name = data.get('father_name') or data.get('father')
    mother_name = data.get('mother_name') or data.get('mother')
    address = data.get('address')
    age = None
    try:
        age = int(data.get('age')) if data.get('age') else None
    except ValueError:
        age = None

    try:
        # read table schema to decide which columns to insert
        cur.execute("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'registration'
        """)
        schema = {row[0]: row[1] for row in cur.fetchall()}

        # prepare candidate values keyed by column name
        candidates = {
            'full_name': full_name,
            'user_id': None,       # will fill below based on type
            'username': username,
            'password_hash': password_hash,
            'email': email,
            'phone': phone,
            'father_name': father_name,
            'mother_name': mother_name,
            'address': address,
            'age': age
        }

        # handle user_id depending on DB type
        if 'user_id' in schema:
            dt = schema['user_id'] or ''
            # if integer type expected, try to parse raw_user_id; if not parseable, leave NULL so DB default can apply
            if 'int' in dt:
                try:
                    candidates['user_id'] = int(raw_user_id) if raw_user_id is not None else None
                except Exception:
                    candidates['user_id'] = None
            else:
                candidates['user_id'] = raw_user_id or username

        # get nullability/default metadata
        cur.execute("""
            SELECT column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'registration'
        """)
        colmeta = {r[0]: {'is_nullable': r[1], 'default': r[2]} for r in cur.fetchall()}

        # --- INSERT THIS BLOCK ---
        # Robust uniqueness checks (case-insensitive for email) to avoid DB UniqueViolation.
        try:
            # check email case-insensitively (handles indexes that use lower(email))
            if email:
                cur.execute(
                    "SELECT 1 FROM registration WHERE LOWER(email) = LOWER(%s) LIMIT 1",
                    (email,)
                )
                if cur.fetchone():
                    try:
                        conn.rollback()
                    except Exception:
                        log.exception("Rollback failed after pre-insert duplicate email check")
                    return (f"<h2>Email already registered</h2>"
                            f"<p>The email {email} is already in use. If this is your account, please <a href='/login'>log in</a>."
                            "<br><a href='/'>Go Back</a></p>"), 409

            # check username (adjust column name if your DB uses a different column for username)
            if username:
                cur.execute("SELECT 1 FROM registration WHERE username = %s LIMIT 1", (username,))
                if cur.fetchone():
                    try:
                        conn.rollback()
                    except Exception:
                        log.exception("Rollback failed after pre-insert duplicate username check")
                    return (f"<h2>Username already taken</h2>"
                            f"<p>The username {username} is already in use. Choose another username or <a href='/login'>log in</a>."
                            "<br><a href='/'>Go Back</a></p>"), 409
        except Exception:
            # If the read check fails, log and proceed — the insert will be caught and handled.
            log.exception("Pre-insert uniqueness check failed")
        # --- END INSERT BLOCK ---

        # build final insert columns/values from schema intersection
        insert_cols = []
        values = []
        desired_order = ['full_name','user_id','username','password_hash','email','phone','father_name','mother_name','address','age']
        for col in desired_order:
            if col in schema:
                val = candidates.get(col)
                meta = colmeta.get(col)
                # If value is None but column has a default, omit it so the DB default (e.g. sequence) is applied.
                if val is None and meta and meta['default'] is not None:
                    continue
                insert_cols.append(col)
                values.append(val)

        if not insert_cols:
            return "<h2>Error:</h2><pre>No matching columns found in registration table.</pre><a href='/'>Go Back</a>", 500

        # Validate required (NOT NULL) columns that have no default: fail early with a clear message.
        for i, col in enumerate(insert_cols):
            meta = colmeta.get(col)
            val = values[i]
            if meta and meta['is_nullable'] == 'NO' and meta['default'] is None and val is None:
                # Required DB column missing value — tell the user or change DB schema
                return (f"<h2>Error:</h2><pre>Column '{col}' is required by the database but no value was provided."
                        f" Provide a value in the form or alter the DB to use a default/sequence.</pre>"
                        "<a href='/'>Go Back</a>"), 400

        placeholders = ','.join(['%s'] * len(values))
        sql = f"INSERT INTO registration ({', '.join(insert_cols)}) VALUES ({placeholders})"
        cur.execute(sql, tuple(values))
        conn.commit()
        log.info("Inserted registration (cols=%s) for user=%s", insert_cols, username or raw_user_id)
        return redirect('/success')

    except psycopg2.errors.UniqueViolation as ue:
        # specific friendly error for unique constraint (email/username etc.)
        try:
            conn.rollback()
        except Exception:
            log.exception("Rollback failed after UniqueViolation")
        log.warning("Unique constraint violation: %s", ue)
        return (f"<h2>Duplicate value</h2><p>A record with that email or unique field already exists."
                f" If this is your account, please log in. Error: {ue}</p><a href='/'>Go Back</a>"), 400

    except psycopg2.IntegrityError as ie:
        # other integrity errors (not-null, foreign key, etc.)
        try:
            conn.rollback()
        except Exception:
            log.exception("Rollback failed after IntegrityError")
        log.exception("Integrity error saving data")
        return f"<h2>Data integrity error</h2><pre>{ie}</pre><a href='/'>Go Back</a>", 400

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            log.exception("Rollback failed")
        log.exception("Error saving data to DB")
        return f"<h2>Error saving data:</h2><pre>{e}</pre><p>Received data: {dict(data)}</p><a href='/'>Go Back</a>"

@app.route('/success')
def success():
    return (
        '<h2>Thanks! Your info has been saved.</h2>'
        '<p><a href="/home_loggedin">Go to Home (logged in)</a></p>'
        '<p><a href="/">Go Back to Form</a></p>'
        '<script>setTimeout(()=>{ window.location.href="/home_loggedin"; }, 2000);</script>'
    )

@app.route('/home_loggedin')
def home_loggedin():
    # serve the static home_loggedin.html from the project folder
    return send_from_directory(os.path.dirname(__file__), 'home_loggedin.html')

@app.route('/db_status')
def db_status():
    if cur is None:
        return f'DB connection error: {db_connect_error}', 500
    try:
        cur.execute('SELECT 1')
        _ = cur.fetchone()
        return 'DB OK', 200
    except Exception as e:
        return f'DB error: {e}', 500

if __name__ == '__main__':
    try:
        log.info("Calling app.run(host=127.0.0.1, port=5000)")
        # bind to localhost explicitly; keep debug on for development
        app.run(debug=True, host='127.0.0.1', port=5000, use_reloader=False)
    except Exception as e:
        log.exception("Unhandled exception running Flask")
        raise