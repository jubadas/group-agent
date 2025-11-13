# duma_whatsapp_bot.py
# Duma WhatsApp demo + OpenAI integration
# IMPORTANT: monitor OpenAI & Twilio usage and follow platform terms.
import os
import difflib
import threading
import time
import traceback
from datetime import datetime
from collections import deque, defaultdict

from flask import Flask, request, jsonify, render_template, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import parsedatetime as pdt
from dotenv import load_dotenv

# AI
import openai
import requests

# Load environment
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # change if needed

openai.api_key = OPENAI_API_KEY

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# Admin
ADMIN_PHONE = os.getenv("ADMIN_PHONE", None)

# Rate limiting
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "1.0"))

# Flask app
app = Flask(__name__, template_folder="templates", static_folder="static")

# ========== Shared State ==========
reminders = []   # {"user": str, "text": str, "time": datetime}
reminders_lock = threading.Lock()

messages = deque(maxlen=200)   # global human chat history (big chat)
group_members = set()          # keep joined phone numbers
cal = pdt.Calendar()

# Per-user rate-limiting
last_request_ts = defaultdict(lambda: 0.0)

# Simple in-memory usage counters (for pilot)
usage_counters = defaultdict(int)

# ========== Slang + Commands (your original maps) ==========
slang_map = {
    "sasa": "hi", "niaje": "hi", "mambo": "hi", "vipi": "hi",
    "poa": "fine", "fiti": "fine", "sawa": "ok", "kwema": "ok",
    "msaada": "help", "huduma": "services", "matukio": "events",
    "mawasiliano": "contact", "kuhusu": "about", "karibu": "welcome",
    "kumbusho": "reminder", "ratiba": "timetable", "magonjwa": "disease",
    "dawa": "drugs"
}

valid_commands = {
    "1": "about", "about": "about",
    "2": "services", "services": "services",
    "3": "contact", "contact": "contact",
    "4": "events", "events": "events",
    "5": "timetable", "timetable": "timetable",
    "6": "notes", "notes": "notes",
    "7": "disease", "disease": "disease",
    "8": "add reminder", "add reminder": "add reminder",
    "9": "show reminders", "show reminders": "show reminders",
    "10": "chat", "chat": "chat",
    "11": "join", "join": "join",
    "menu": "menu", "help": "menu",
    "hi": "hi", "hello": "hi", "ok": "hi", "fine": "hi"
}

disease_info = {
    "anthrax": {
        "cause": "Bacillus anthracis (bacteria)",
        "symptoms": "Sudden death, bleeding from body openings, swelling of neck/chest.",
        "treatment": "No effective treatment once acute, but vaccination prevents outbreaks.",
        "prevention": "Annual vaccination, proper disposal of carcasses."
    },
    "foot and mouth": {
        "cause": "FMD virus (highly contagious)",
        "symptoms": "Fever, blisters in mouth/hooves, drooling, lameness.",
        "treatment": "No cure, supportive care only.",
        "prevention": "Movement control, vaccination, strict biosecurity."
    },
    "rabies": {
        "cause": "Rabies virus (transmitted by bites).",
        "symptoms": "Behavior change, aggression, paralysis, death.",
        "treatment": "Fatal once signs appear.",
        "prevention": "Dog vaccination, post-exposure prophylaxis."
    },
    "east coast fever": {
        "cause": "Protozoa (Theileria parva) spread by brown ear tick.",
        "symptoms": "Fever, swollen lymph nodes, breathing problems, death in cattle.",
        "treatment": "Drugs like buparvaquone if given early.",
        "prevention": "Tick control, ECF vaccination."
    },
    "brucellosis": {
        "cause": "Brucella bacteria.",
        "symptoms": "Abortions, retained placenta, infertility.",
        "treatment": "No effective treatment in livestock.",
        "prevention": "Vaccination, testing and culling, avoid raw milk."
    }
}

# ========== Helpers ==========
def normalize_text(text):
    if not text:
        return ""
    text = text.strip().lower()
    words = text.split()
    normalized = [slang_map.get(w, w) for w in words]
    joined = " ".join(normalized)

    if joined in valid_commands:
        return valid_commands[joined]

    closest = difflib.get_close_matches(joined, list(valid_commands.keys()), n=1, cutoff=0.7)
    if closest:
        return valid_commands[closest[0]]

    return joined

def broadcast_message(sender, text):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    messages.append(f"{stamp} • {sender}: {text}")

def explain_reminder():
    return (
        "📝 *How to set a reminder:*\n"
        "• `add reminder exam tomorrow at 9am`\n"
        "• `remind me to call John at 5pm`\n"
        "• `set reminder buy drugs Monday 10am`\n\n"
        "✅ I’ll remember it and remind you when the time comes!"
    )

def parse_reminder_time(text):
    try:
        time_struct, parse_status = cal.parse(text)
        if parse_status == 0:
            return None, None
        reminder_time = datetime(*time_struct[:6])
        return reminder_time, text
    except Exception:
        return None, None

# ========== Reminder worker ==========
def reminder_loop():
    while True:
        try:
            now = datetime.now()
            to_send = []
            with reminders_lock:
                for r in list(reminders):
                    if r["time"] <= now:
                        to_send.append(r)
                for r in to_send:
                    try:
                        # send via Twilio if configured
                        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
                            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                            client.messages.create(
                                from_=TWILIO_WHATSAPP_NUMBER,
                                to=f"whatsapp:{r['user']}",
                                body=f"⏰ Reminder: {r['text']}"
                            )
                        print(f"⏰ Sent reminder to {r['user']}: {r['text']}")
                    except Exception as send_err:
                        print("Error sending reminder to", r['user'], send_err)
                    try:
                        reminders.remove(r)
                    except ValueError:
                        pass
        except Exception:
            traceback.print_exc()
        time.sleep(15)

threading.Thread(target=reminder_loop, daemon=True).start()

# ========== AI integration ==========
def ai_generate_reply(user_id, user_message, context_lines=6):
    """
    Calls OpenAI to produce a short, helpful assistant reply.
    Keeps context short — uses the latest `messages` as the group chat context.
    """
    # Rate-limiting (safe guard for OpenAI calls)
    now_ts = time.time()
    last_ts = last_request_ts.get(user_id, 0.0)
    if now_ts - last_ts < RATE_LIMIT_SECONDS:
        wait = RATE_LIMIT_SECONDS - (now_ts - last_ts)
        time.sleep(wait + 0.01)
    last_request_ts[user_id] = time.time()
    usage_counters[user_id] += 1

    # Construct a short context from the last few messages for group awareness
    context = list(messages)[-context_lines:]
    system_prompt = (
        "You are Duma — a concise, helpful assistant for animal health students. "
        "Give short actionable answers. If asked to set reminders, echo the parsed time format. "
        "If asked for non-medical advice, warn and suggest seeking a vet or lecturer. "
        "Use plain language; keep reply under 220 words."
    )

    # Build the chat messages for the API
    chat_messages = [
        {"role": "system", "content": system_prompt}
    ]
    # Add recent group messages as context
    if context:
        chat_messages.append({"role": "system", "content": "Recent class chat:\n" + "\n".join(context)})

    # Add user content
    chat_messages.append({"role": "user", "content": user_message})

    # Call OpenAI Chat API (Chat Completion)
    try:
        resp = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=chat_messages,
            max_tokens=300,
            temperature=0.1,
            n=1,
        )
        assistant_text = resp.choices[0].message["content"].strip()
        return assistant_text
    except Exception as e:
        traceback.print_exc()
        # Fallback minimal reply
        return "Sorry — I'm having trouble reaching my AI brain. Try again in a moment."

# ========== Flask routes ==========
@app.route("/")
def index():
    return "Duma demo is running. Use /webchat for browser UI or POST /whatsapp to simulate."

@app.route("/webchat", methods=["GET"])
def webchat_ui():
    # Simple HTML two-pane UI served from templates/webchat.html if present
    try:
        return render_template("webchat.html")
    except Exception:
        # fallback minimal page
        html = """
        <html><head><title>Duma Webchat</title></head><body>
        <h3>Duma Web Chat (basic)</h3>
        <p>POST to /api/send {phone, message} to send a message; /api/history to view chat history.</p>
        </body></html>
        """
        return html

@app.route("/api/history", methods=["GET"])
def api_history():
    # return last 100 messages, and current group members
    return jsonify({
        "messages": list(messages)[-100:],
        "members": list(group_members),
        "usage_counters": dict(list(usage_counters.items())[:20])
    })

@app.route("/api/send", methods=["POST"])
def api_send():
    """
    Test endpoint for sending a message as if from WhatsApp.
    JSON: { "from": "whatsapp:+2547...", "body": "hi" }
    """
    data = request.get_json(force=True)
    incoming = data.get("body", "")
    sender = data.get("from", "web_user")
    # emulate Twilio format
    form = {"Body": incoming, "From": sender}
    # Call internal handler
    resp = whatsapp_logic(form)
    return jsonify({"reply": resp})

# Keep main Twilio webhook for real testing (Twilio sandbox)
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    # Twilio will POST form-encoded data
    resp_xml = whatsapp_logic(request.form)
    return resp_xml

# Core logic isolated for re-use
def whatsapp_logic(form):
    incoming_msg = form.get("Body", "") or ""
    sender_raw = form.get("From", "") or ""
    sender = sender_raw.split(":")[-1] if ":" in sender_raw else sender_raw
    command = normalize_text(incoming_msg)

    # Quick logging
    broadcast_message(sender, incoming_msg)

    # Twilio response builder (TwiML)
    resp = MessagingResponse()
    msg = resp.message()

    # Quick checks for some keywords
    if "how to set reminder" in incoming_msg.lower():
        msg.body(explain_reminder())
        return str(resp)

    if command == "hi":
        msg.body(f"👋 Hello {sender}!\nI’m *Duma*, your Animal Health Class Assistant. Type `menu` to see options.")
        return str(resp)

    if command == "menu":
        msg.body("📌 *Duma Animal Health Menu:*\n1 about 2 services 3 contact 4 events 5 timetable 6 notes 7 disease 8 add reminder 9 show reminders 10 chat 11 join")
        return str(resp)

    if command == "about":
        msg.body("🏫 Duma is a demo AI for animal health students. Type 'menu' to go back.")
        return str(resp)

    if command == "services":
        msg.body("🛠 Services: Vaccination drives, diagnostic guides, field training info.")
        return str(resp)

    if command == "contact":
        msg.body("📞 Admin: Caleb Kasura\nEmail: calebkasura6@gmail.com")
        return str(resp)

    if command == "events":
        msg.body("📅 Events: Field visit Sep15, Guest lecture Sep30, Practical Oct10")
        return str(resp)

    if command == "timetable":
        msg.body("📚 Timetable: Mon-Parasitology 10am / Tue-Anatomy 2pm / Wed-Physiology 9am / Thu-Pathology 11am / Fri-Lab 1pm")
        return str(resp)

    # NOTES & DISEASES
    if command.startswith("notes"):
        topic = incoming_msg.lower().replace("notes", "").replace("6", "").strip()
        if not topic:
            msg.body("📖 Use: `notes <topic>`\nExample: `notes parasites`")
        else:
            msg.body(f"📖 Notes on {topic.title()} (summary):\n[Short summary placeholder].")
        return str(resp)

    if command.startswith("disease"):
        disease = incoming_msg.lower().replace("disease", "").replace("7", "").strip()
        if not disease:
            msg.body("Use: `disease <name>`")
            return str(resp)
        if disease in disease_info:
            info = disease_info[disease]
            msg.body(f"🦠 {disease.title()} — Cause: {info['cause']} Symptoms: {info['symptoms']} Prevention: {info['prevention']}")
        else:
            msg.body(f"⚠️ I don't have info on {disease.title()}. Try anthrax, rabies, foot and mouth.")
        return str(resp)

    # REMINDERS
    lowered = incoming_msg.lower()
    if lowered.startswith("add reminder") or lowered.startswith("remind me") or lowered.startswith("set reminder"):
        reminder_text = lowered.replace("add reminder", "").replace("remind me", "").replace("set reminder", "").replace("8", "").strip()
        if not reminder_text:
            msg.body("⚠️ Use format: `add reminder <your reminder + time>`\n\n" + explain_reminder())
            return str(resp)
        reminder_time, parsed = parse_reminder_time(reminder_text)
        if reminder_time:
            with reminders_lock:
                reminders.append({"user": sender, "text": reminder_text, "time": reminder_time})
            msg.body(f"✅ Reminder saved for {sender} at {reminder_time}.")
        else:
            msg.body("⚠️ Couldn't parse the time. Try: 'add reminder exam tomorrow at 9am'\n\n" + explain_reminder())
        return str(resp)

    if command == "show reminders":
        with reminders_lock:
            user_reminders = [r for r in reminders if r["user"] == sender]
        if user_reminders:
            lines = [f"⏰ {r['text']} → {r['time']}" for r in user_reminders]
            msg.body("📝 Your Reminders:\n" + "\n".join(lines))
        else:
            msg.body("📭 You don't have reminders. Use 'add reminder ...'")
        return str(resp)

    # CHAT: big human chat vs small AI assistant
    if command == "join":
        group_members.add(sender)
        msg.body(f"✅ {sender} joined the class chat! 🎉 Use 'chat <message>' to speak.")
        return str(resp)

    if command.startswith("chat"):
        # human message — goes to big class chat; AI replies remain small assistant replies
        user_text = incoming_msg.replace("chat", "").replace("10", "").strip()
        if not user_text:
            msg.body("⚠️ Use: chat <message>")
            return str(resp)
        if sender not in group_members:
            msg.body("⚠️ You must `join` first before using chat.")
            return str(resp)

        # Add human-sourced message to big chat history
        broadcast_message(sender, user_text)

        # Build a short AI assistant reply (this is the small pane reply)
        ai_prompt = f"Student {sender} wrote: {user_text}\nBe concise and helpful. If the user asks for scheduling or reminders, give a clear format."
        assistant_reply = ai_generate_reply(sender, ai_prompt)

        # Save assistant's small reply into messages as tagged AI response
        broadcast_message("Duma (AI)", assistant_reply)

        # Return a short TwiML containing both the last few chat messages and the AI reply
        recent = "\n".join(list(messages)[-6:])
        response_text = f"💬 (latest chat)\n{recent}\n\n🤖 (Duma reply)\n{assistant_reply}"
        msg.body(response_text)
        return str(resp)

    # If message doesn't match anything — fallback to AI small assistant
    if incoming_msg.strip():
        # Let AI handle unknown free-form queries, but limit token usage
        ai_prompt = f"User {sender} asks: {incoming_msg}\nRespond concisely (max 220 words). If it's a personal health or safety issue, advise to speak to a vet or lecturer."
        assistant_reply = ai_generate_reply(sender, ai_prompt)
        broadcast_message("Duma (AI)", assistant_reply)
        msg.body(assistant_reply)
        return str(resp)

    # default fallback
    msg.body("🤖 I didn't understand. Type 'menu' for options.")
    return str(resp)

# ========== Run server ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    # Twilio client warmup omitted until needed
    print("Starting Duma demo (AI-enabled). Listening on port", port)
    app.run(host="0.0.0.0", port=port, debug=False)
