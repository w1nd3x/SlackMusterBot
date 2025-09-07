import os
import logging
import schedule
import time
import sqlite3
from datetime import datetime, timedelta, date
from slack_bolt import App, Ack
from slack_bolt.adapter.socket_mode import SocketModeHandler
from threading import Thread
from dateutil.parser import parse
from calendar import month_name, monthrange

# --- Configuration ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID") # The ID of the channel to post in
REPORTING_USER_ID = os.environ.get("REPORTING_USER_ID") # The user ID to send the summary report to
DATABASE_FILE = os.environ.get("DATABASE_FILE")

# --- Globals --- 
daily_thread_ts = {}

# --- Initialization ---
logging.basicConfig(level=logging.INFO)
app = App(token=SLACK_BOT_TOKEN)

# --- Database Setup ---
def db_connect():
    return sqlite3.connect(DATABASE_FILE) # type: ignore

def setup_database():
    """Initializes the SQLite database and creates tables if they don't exist."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
            response_date TEXT NOT NULL, response_text TEXT NOT NULL, details TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, 
            destination_id TEXT NOT NULL, sent_timestamp TEXT NOT NULL, message TEXT NOT NULL
        )               
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leave (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, user_name TEXT NOT NULL, 
            start_date TEXT NOT NULL, end_date TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS holidays (
            holiday_date TEXT PRIMARY KEY, description TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id TEXT PRIMARY KEY
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )
    ''')
    # Seed initial data if tables are empty
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('checkin_time', '08:00')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('reminder_time', '10:00')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('summary_time', '11:00')")
    # Add the reporting user as the first admin
    if REPORTING_USER_ID:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (REPORTING_USER_ID,))
    conn.commit()
    conn.close()
    logging.info("Database initialized.")

# --- Helper Functions ---
def is_admin(user_id):
    """Checks if a user_id is in the admins table."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    is_admin_user = cursor.fetchone() is not None
    conn.close()
    return is_admin_user

def is_workday(check_date):
    """Checks if a given date is a workday (not weekend or holiday)."""
    if check_date.weekday() >= 5: # Saturday or Sunday
        return False
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM holidays WHERE holiday_date = ?", (check_date.strftime("%Y-%m-%d"),))
    is_holiday = cursor.fetchone() is not None
    conn.close()
    return not is_holiday

def get_channel_members(channel_id):
    """Fetches a list of all non-bot members from a given channel."""
    try:
        result = app.client.conversations_members(channel=channel_id)
        member_ids = result["members"]

        # Filter out bots
        human_members = []
        for user_id in member_ids: # type: ignore
            user_info = app.client.users_info(user=user_id)
            if not user_info["user"]["is_bot"]: # type: ignore
                human_members.append(user_id)
        return human_members
    except Exception as e:
        logging.error(f"Error fetching channel members: {e}")
        return []

def is_user_on_leave(user_id, check_date):
    """Checks if a user is on leave on a specific date."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT start_date, end_date FROM leave WHERE user_id = ?", (user_id,))
    leave_periods = cursor.fetchall()
    conn.close()

    for start_str, end_str in leave_periods:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
        if start_date <= check_date <= end_date:
            return True
    return False

# --- Core Bot Logic ---
def post_daily_checkin(ignore_off_day=False):
    """Posts the daily check-in message to the target channel."""
    today = date.today() # Define today's date

    if not ignore_off_day and not is_workday(today):
        logging.info("It's a holiday or weekend, no check-in message sent.")        
        return

    try:
        # The rest of the message posting logic remains similar
        result = app.client.chat_postMessage(
            channel=TARGET_CHANNEL_ID, # type: ignore
            text="Good morning team! Please check in for the day.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "*Good morning! :sunrise: Please check in for today.*"}},
                {"type": "actions", "block_id": "check_in_actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "In at Normal Time"}, "style": "primary", "action_id": "action_in_normal"},
                    {"type": "button", "text": {"type": "plain_text", "text": "In Late"}, "action_id": "action_in_late"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Working from Home"}, "action_id": "action_wfh"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Appointment"}, "action_id": "action_appointment"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Out Sick"}, "style": "danger", "action_id": "action_out_sick"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Liberty"}, "action_id": "action_liberty"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Other..."}, "action_id": "action_other"}
                ]}
            ]

        )
        daily_thread_ts[today.strftime("%Y-%m-%d")] = result['ts']
        logging.info(f"Daily check-in message sent to channel {TARGET_CHANNEL_ID}")
    except Exception as e:
        logging.error(f"Error posting daily message: {e}")

def post_daily_summary(ignore_off_day=False):
    today = date.today()
    if not ignore_off_day and not is_workday(today): return

    today_str = today.strftime("%Y-%m-%d")
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, response_text, details FROM responses WHERE response_date = ?", (today_str,))
    responses = cursor.fetchall()
    conn.close()

    if not responses:
        summary_text = f"*Daily Status Summary for {today_str}*\n\nNo one has checked in yet."
    else:
        summary_text = f"*Daily Status Summary for {today_str}*\n"
        for user_id, response, details in responses:
            details_text = f" ({details})" if details else ""
            summary_text += f"\n• <@{user_id}>: *{response}*{details_text}"

    try:
        ts = daily_thread_ts.get(today_str)
        app.client.chat_postMessage(channel=TARGET_CHANNEL_ID, text=summary_text, thread_ts=ts) # type: ignore
        logging.info("Posted daily summary.")
    except Exception as e:
        logging.error(f"Failed to post daily summary: {e}")

def post_reminders(ignore_off_day=False):
    """Sends reminders to users who have not checked in."""
    today = date.today()
    if not ignore_off_day and not is_workday(today):
        return

    today_str = today.strftime("%Y-%m-%d")
    
    try:
        all_channel_members = get_channel_members(TARGET_CHANNEL_ID)
        
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM responses WHERE response_date = ?", (today_str,))
        responded_users = {row[0] for row in cursor.fetchall()}
        conn.close()

        missing_users = set(all_channel_members) - responded_users

        for user_id in missing_users:
            if not is_user_on_leave(user_id, today):
                app.client.chat_postMessage(
                    channel=user_id,
                    text="Just a friendly reminder to please check in for today! ☀️"
                )
                logging.info(f"Sent reminder to {user_id}")

    except Exception as e:
        logging.error(f"Failed to send reminders: {e}")

# --- Slack Action & View Handlers --- 
def handle_response(body, response_text, details=None):
    user_id = body["user"]["id"]
    user_name = body["user"]["name"]  # Get the user's name
    today_str = date.today().strftime("%Y-%m-%d")
    thread_ts = daily_thread_ts.get(today_str)

    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO responses (user_id, user_name, response_date, response_text, details) VALUES (?, ?, ?, ?, ?)",
            (user_id, user_name, today_str, response_text, details)
        )
        conn.commit()
        conn.close()

        details_text = f" (Details: {details})" if details else ""
        app.client.chat_postMessage(
            channel=TARGET_CHANNEL_ID, # type: ignore
            thread_ts=thread_ts,
            text=f"<@{user_id}> has checked in: *{response_text}*{details_text}"
        )
    except Exception as e:
        logging.error(f"Error handling response: {e}")

@app.action("action_in_normal")
@app.action("action_wfh")
@app.action("action_out_sick")
@app.action("action_liberty")
def handle_simple_checkin(ack: Ack, body, logger, action):
    """Handles button clicks that don't require a modal."""
    ack()
    response_map = {
        "action_in_normal": "In at Normal Time",
        "action_wfh": "Working from Home",
        "action_out_sick": "Out Sick",
        "action_liberty": "Liberty"
    }
    handle_response(body, response_map[action["action_id"]])

@app.action("action_in_late")
@app.action("action_appointment")
@app.action("action_other")
def handle_modal_checkin(ack: Ack, body, client, action):
    ack()
    action_map = {
        "action_in_late": {"title": "In Late", "label": "What time do you expect to be in?", "placeholder": "e.g., 10:30 AM"},
        "action_appointment": {"title": "Appointment", "label": "What are the details of the appointment?", "placeholder": "e.g., Dentist at 2 PM"},
        "action_other": {"title": "Other Status", "label": "Please provide your status for the day.", "placeholder": "e.g., Working from the airport"}
    }
    config = action_map[action["action_id"]]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": f"modal_submit_{action['action_id']}",
            "title": {"type": "plain_text", "text": config["title"]},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [{"type": "input","block_id": "details_block",
                        "element": {"type": "plain_text_input","action_id": "details_input", "placeholder": {"type": "plain_text", "text": config["placeholder"]}},
                        "label": {"type": "plain_text", "text": config["label"]}}]
        }
    )

@app.view("modal_submit_action_in_late")
@app.view("modal_submit_action_appointment")
@app.view("modal_submit_action_other")
def handle_modal_submission(ack: Ack, body, view):
    """Handles the submission of the 'late arrival time' modal."""
    ack()
    response_map = {
        "modal_submit_action_in_late": "In Late", "modal_submit_action_appointment": "Appointment",
        "modal_submit_action_other": "Other"
    }
    response_text = response_map[view["callback_id"]]
    details = view["state"]["values"]["details_block"]["details_input"]["value"]
    handle_response(body, response_text, details)

@app.event("message")
def handle_message_events(body, logger):
    """Logs all messages to the database."""
    event = body.get("event", {})
    message_text = event.get("text")
    sender_id = event.get("user")
    destination_id = event.get("channel")
    sent_timestamp = event.get("ts")

    # To get the sender's name, you need to make an API call
    try:
        user_info = app.client.users_info(user=sender_id)
        sender_name = user_info["user"]["name"] # type: ignore
    except Exception as e:
        logger.error(f"Error fetching user info: {e}")
        sender_name = "Unknown"


    if message_text and sender_id and destination_id and sent_timestamp:
        try:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (sender_id, sender_name, destination_id, sent_timestamp, message) VALUES (?, ?, ?, ?, ?)",
                (sender_id, sender_name, destination_id, sent_timestamp, message_text)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error logging message to database: {e}")

# --- Slash Command Handlers --- 
def register_commands(app):

    @app.command("/post_checkin")
    def handle_post_checkin(ack, body, say):
        """Manually posts the daily check-in message."""
        ack()
        if not is_admin(body["user_id"]):
            say("Sorry, only admins can use this command.")
            return
        
        say(text="Forcing the daily check-in post now...", ephemeral=True)
        post_daily_checkin(True)

    @app.command("/post_reminders")
    def handle_post_reminders(ack, body, say):
        """Manually sends check-in reminders."""
        ack()
        if not is_admin(body["user_id"]):
            say("Sorry, only admins can use this command.")
            return
            
        say(text="Forcing reminder DMs now...", ephemeral=True)
        post_reminders(True)

    @app.command("/post_summary")
    def handle_post_summary(ack, body, say):
        """Manually posts the daily summary message."""
        ack()
        if not is_admin(body["user_id"]):
            say("Sorry, only admins can use this command.")
            return
        
        say(text="Forcing the daily summary post now...", ephemeral=True)
        post_daily_summary(True)

    @app.command("/timeoff")
    def handle_timeoff(ack, body, client, logger):
        """Opens a modal for the user to register their leave dates."""
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "leave_modal",
                    "title": {"type": "plain_text", "text": "Register Leave/PTO"},
                    "submit": {"type": "plain_text", "text": "Submit"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "start_date_block",
                            "element": {"type": "datepicker", "action_id": "start_date_picker"},
                            "label": {"type": "plain_text", "text": "Start Date"}
                        },
                        {
                            "type": "input",
                            "block_id": "end_date_block",
                            "element": {"type": "datepicker", "action_id": "end_date_picker"},
                            "label": {"type": "plain_text", "text": "End Date"}
                        }
                    ]
                }
            )
        except Exception as e:
            logger.error(f"Error opening 'leave' modal: {e}")

    @app.view("leave_modal")
    def handle_leave_submission(ack, body, logger):
        """Handles the submission of the leave registration modal."""
        ack()
        user_id = body["user"]["id"]
        user_name = body["user"]["name"] # Get the user's name
        values = body["view"]["state"]["values"]
        start_date = values["start_date_block"]["start_date_picker"]["selected_date"]
        end_date = values["end_date_block"]["end_date_picker"]["selected_date"]

        if not start_date or not end_date or (parse(end_date) < parse(start_date)):
            # You can also send an ephemeral message back to the user here
            logger.error("Invalid date range submitted for leave.")
            return

        try:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO leave (user_id, user_name, start_date, end_date) VALUES (?, ?, ?, ?)",
                (user_id, user_name, start_date, end_date)
            )
            conn.commit()
            conn.close()
            app.client.chat_postEphemeral(
                channel=TARGET_CHANNEL_ID,
                user=user_id,
                text=f"Got it! Your leave from {start_date} to {end_date} has been recorded."
            )
        except Exception as e:
            logger.error(f"Database error on leave submission: {e}")

    @app.command("/calendar")
    def handle_calendar(ack, body, say):
        ack()
        say("Calendar feature coming soon!")

    @app.command("/status")
    def handle_status(ack, body, say):
        ack()
        parts = body["text"].split(" ", 1)
        if len(parts) < 2:
            say("Usage: `/status [@user|team]`")
            return
        
    @app.command("/help")
    def handle_help(ack, body, say):
        ack()
        help_text = (
            "*Muster Bot Commands*\n"
            "`/timeoff` - Register your upcoming leave or PTO.\n"
            "`/calendar` - Display a calendar of team leave and holidays for the month.\n"
            "`/team_status` - Check the status of the whole team.\n"
            "`/user_status [@user]` - Check the status of a team member.\n"            
            "`/help` - Shows this message.\n\n"
        )
        if is_admin(body["user_id"]):
            help_text += (
            "*Admin Commands*\n"
            "`/holiday [YYYY-MM-DD] [description]` - Add a company holiday.\n"
            "`/edit_status [@user] [status]` - Manually set a user's status for today.\n"
            "`/add_admin [@user]` - Grant a user admin permissions for this bot.\n"
            "`/report [@user] [period]` - Generate an accountability report.\n"
            "`/config [key] [value]` - View or set bot configuration.\n"
        )
        say(text=help_text)

    @app.command("/holiday")
    def handle_holiday(ack: Ack, body, say):
        ack()
        if not is_admin(body["user_id"]):
            say("Sorry, only admins can use this command.")
            return
        
        parts = body["text"].split(" ", 1)
        if len(parts) < 2:
            say("Usage: `/holiday YYYY-MM-DD Description`")
            return
        
        holiday_date, description = parts
        try:
            parsed_date = parse(holiday_date).strftime("%Y-%m-%d")
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO holidays (holiday_date, description) VALUES (?, ?)", (parsed_date, description))
            conn.commit()
            conn.close()
            say(f":tada: Holiday '{description}' on {parsed_date} has been added.")
        except Exception as e:
            say(f"Sorry, I couldn't understand that date. Please use a format like YYYY-MM-DD. Error: {e}")
    
    @app.command("/add_admin")
    def handle_add_admin(ack, body, say):
        ack()
        say("Add admin feature coming soon!")

    @app.command("/edit_status")
    def handle_edit_status(ack, body, say):
        ack()
        say("Edit status feature coming soon!")

    @app.command("/report")
    def handle_report(ack, body, say):
        ack()
        say("Report feature coming soon!")

    @app.command("/config")
    def handle_config(ack, body, say):
        ack()
        say("Config feature coming soon!")

# --- Scheduling ---
def run_schedule():
    """Sets up and runs the scheduled tasks based on config."""
    conn = db_connect()
    cursor = conn.cursor()
    configs = {k: v for k, v in cursor.execute("SELECT key, value FROM config").fetchall()}
    conn.close()

    # Schedule tasks using times from the database
    schedule.every().monday.at(configs.get('checkin_time', '08:00')).do(post_daily_checkin)
    schedule.every().tuesday.at(configs.get('checkin_time', '08:00')).do(post_daily_checkin)
    schedule.every().wednesday.at(configs.get('checkin_time', '08:00')).do(post_daily_checkin)
    schedule.every().thursday.at(configs.get('checkin_time', '08:00')).do(post_daily_checkin)
    schedule.every().friday.at(configs.get('checkin_time', '08:00')).do(post_daily_checkin)
    
    schedule.every().day.at(configs.get('summary_time', '11:00')).do(post_daily_summary)
    schedule.every().day.at(configs.get('reminder_time', '10:00')).do(post_reminders) # Use the post_reminders function

    logging.info("Scheduler configured and running.")
    while True:
        schedule.run_pending()
        time.sleep(1)

# --- App Entry Point ---
if __name__ == "__main__":
    setup_database()
    register_commands(app)

    scheduler_thread = Thread(target=run_schedule)
    scheduler_thread.daemon = True
    scheduler_thread.start()

    SocketModeHandler(app, SLACK_APP_TOKEN).start()
