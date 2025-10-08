from flask import Flask, request, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

def get_calendar_service():
    """
    Create authenticated Google Calendar service
    Auto-refreshes access token using refresh token
    """
    creds = Credentials(
        token=None,  # Will be auto-generated
        refresh_token=os.getenv('GOOGLE_REFRESH_TOKEN'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.getenv('GOOGLE_CLIENT_ID'),
        client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    
    # This automatically gets a fresh access token
    if not creds.valid:
        creds.refresh(Request())
    
    service = build('calendar', 'v3', credentials=creds)
    return service


# WEBHOOK 1: CHECK AVAILABILITY

@app.route('/webhook/check-availability', methods=['POST'])
def check_availability():
    """Check free time slots for a date"""
    try:
        data = request.json
        date_str = data.get('date', '').strip()
        timezone = data.get('timezone', 'Asia/Kolkata')
        
        if not date_str:
            return jsonify({
                'success': False,
                'error': 'date parameter required'
            }), 400
        
        # Parse date - handle natural language
        date_str_lower = date_str.lower()
        if date_str_lower == 'today':
            target_date = datetime.now()
        elif date_str_lower == 'tomorrow':
            target_date = datetime.now() + timedelta(days=1)
        else:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d')
            except:
                return jsonify({
                    'success': False,
                    'error': 'Invalid date format. Use YYYY-MM-DD, "today", or "tomorrow"'
                }), 400
        
        # Working hours: 9 AM to 6 PM
        start_time = target_date.replace(hour=9, minute=0, second=0, microsecond=0)
        end_time = target_date.replace(hour=18, minute=0, second=0, microsecond=0)
        
        # Get calendar service (auto-refreshes token)
        service = get_calendar_service()
        
        # Query busy times
        body = {
            'timeMin': start_time.isoformat() + 'Z',
            'timeMax': end_time.isoformat() + 'Z',
            'items': [{'id': 'primary'}],
            'timeZone': timezone
        }
        
        result = service.freebusy().query(body=body).execute()
        busy_slots = result['calendars']['primary'].get('busy', [])
        
        # Find free 1-hour slots
        free_slots = []
        current = start_time
        
        while current < end_time:
            slot_end = current + timedelta(hours=1)
            
            # Check conflicts
            is_free = True
            for busy in busy_slots:
                busy_start = datetime.fromisoformat(busy['start'].replace('Z', ''))
                busy_end = datetime.fromisoformat(busy['end'].replace('Z', ''))
                
                if current < busy_end and slot_end > busy_start:
                    is_free = False
                    break
            
            if is_free:
                free_slots.append({
                    'start': current.strftime('%Y-%m-%d %H:%M'),
                    'end': slot_end.strftime('%Y-%m-%d %H:%M'),
                    'display': f"{current.strftime('%I:%M %p')} to {slot_end.strftime('%I:%M %p')}"
                })
            
            current = slot_end
        
        return jsonify({
            'success': True,
            'date': target_date.strftime('%Y-%m-%d'),
            'free_slots': free_slots,
            'count': len(free_slots),
            'message': f"Found {len(free_slots)} free slots" if free_slots else "No free slots available"
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# WEBHOOK 2: CREATE EVENT

@app.route('/webhook/create-event', methods=['POST'])
def create_event():
    """Create new calendar event for client"""
    try:
        data = request.json
        
        # Get parameters - now expecting client_name and client_email
        client_name = data.get('client_name', '').strip()
        client_email = data.get('client_email', '').strip()
        start_time = data.get('start_time', '').strip()
        end_time = data.get('end_time', '').strip()
        description = data.get('description', '').strip()
        timezone = data.get('timezone', 'Asia/Kolkata')
        
        # Validation
        if not client_name:
            return jsonify({'success': False, 'error': 'client_name required'}), 400
        if not client_email:
            return jsonify({'success': False, 'error': 'client_email required'}), 400
        if not start_time:
            return jsonify({'success': False, 'error': 'start_time required'}), 400
        if not end_time:
            return jsonify({'success': False, 'error': 'end_time required'}), 400
        
        # Auto-generate title
        title = f"Appointment with {client_name}"
        
        # Build event
        event = {
            'summary': title,
            'description': description if description else f"Appointment booked for {client_name}",
            'start': {
                'dateTime': start_time,
                'timeZone': timezone,
            },
            'end': {
                'dateTime': end_time,
                'timeZone': timezone,
            },
            'attendees': [
                {'email': client_email, 'responseStatus': 'needsAction'}
            ],
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 1440},  # 1 day before
                    {'method': 'popup', 'minutes': 30},
                ],
            },
        }
        
        # Create event (auto-refreshes token)
        service = get_calendar_service()
        created = service.events().insert(
            calendarId='primary',
            body=event,
            sendUpdates='all'  # Always send invite to client
        ).execute()
        
        return jsonify({
            'success': True,
            'event_id': created['id'],
            'event_link': created.get('htmlLink', ''),
            'client_name': client_name,
            'client_email': client_email,
            'message': f"Appointment confirmed for {client_name}. Calendar invite sent to {client_email}",
            'start': created['start']['dateTime'],
            'end': created['end']['dateTime']
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# WEBHOOK 3: LIST EVENTS

@app.route('/webhook/list-events', methods=['POST'])
def list_events():
    """List upcoming events"""
    try:
        data = request.json
        days_ahead = int(data.get('days_ahead', 7))
        max_results = int(data.get('max_results', 10))
        
        # Time range
        now = datetime.utcnow()
        future = now + timedelta(days=days_ahead)
        
        # Get events
        service = get_calendar_service()
        result = service.events().list(
            calendarId='primary',
            timeMin=now.isoformat() + 'Z',
            timeMax=future.isoformat() + 'Z',
            maxResults=max_results,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = result.get('items', [])
        
        # Format
        formatted = []
        for evt in events:
            formatted.append({
                'id': evt['id'],
                'title': evt.get('summary', 'Untitled'),
                'description': evt.get('description', ''),
                'start': evt['start'].get('dateTime', evt['start'].get('date')),
                'end': evt['end'].get('dateTime', evt['end'].get('date')),
                'link': evt.get('htmlLink', '')
            })
        
        return jsonify({
            'success': True,
            'count': len(formatted),
            'events': formatted,
            'message': f"{len(formatted)} upcoming events found" if formatted else "No upcoming events"
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# WEBHOOK 4: CANCEL EVENT

@app.route('/webhook/cancel-event', methods=['POST'])
def cancel_event():
    """Cancel/delete event"""
    try:
        data = request.json
        event_id = data.get('event_id', '').strip()
        
        if not event_id:
            return jsonify({'success': False, 'error': 'event_id required'}), 400
        
        service = get_calendar_service()
        service.events().delete(
            calendarId='primary',
            eventId=event_id,
            sendUpdates='all'
        ).execute()
        
        return jsonify({
            'success': True,
            'message': 'Event cancelled successfully'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# WEBHOOK 5: RESCHEDULE EVENT

@app.route('/webhook/reschedule-event', methods=['POST'])
def reschedule_event():
    """Reschedule existing event"""
    try:
        data = request.json
        event_id = data.get('event_id', '').strip()
        new_start = data.get('new_start_time', '').strip()
        new_end = data.get('new_end_time', '').strip()
        
        if not event_id or not new_start or not new_end:
            return jsonify({
                'success': False,
                'error': 'event_id, new_start_time, new_end_time required'
            }), 400
        
        service = get_calendar_service()
        
        # Get existing event
        event = service.events().get(
            calendarId='primary',
            eventId=event_id
        ).execute()
        
        # Update times
        event['start']['dateTime'] = new_start
        event['end']['dateTime'] = new_end
        
        # Update
        updated = service.events().update(
            calendarId='primary',
            eventId=event_id,
            body=event,
            sendUpdates='all'
        ).execute()
        
        return jsonify({
            'success': True,
            'message': 'Event rescheduled successfully',
            'title': updated['summary'],
            'new_start': updated['start']['dateTime'],
            'new_end': updated['end']['dateTime']
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# HEALTH CHECK

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'message': 'Server running'})

@app.route('/', methods=['GET'])
def home():
    return '''
    <h1>🎙️ Voice Calendar API - LIVE</h1>
    <p>Backend for ElevenLabs Voice Agent</p>
    <h3>Endpoints:</h3>
    <ul>
        <li>POST /webhook/check-availability</li>
        <li>POST /webhook/create-event</li>
        <li>POST /webhook/list-events</li>
        <li>POST /webhook/cancel-event</li>
        <li>POST /webhook/reschedule-event</li>
        <li>GET /health</li>
    </ul>
    <p><strong>Status:</strong>  Ready</p>
    '''


# RUN

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print('=' * 50)
    print(f' Voice Calendar API starting...')
    print(f' Running on port {port}')
    print(f' Local: http://localhost:{port}')
    print('=' * 50)
    app.run(host='0.0.0.0', port=port, debug=False)