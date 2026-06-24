from flask import Flask, request, jsonify
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import requests
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import time
from collections import defaultdict
import random
import os
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import jwt

app = Flask(__name__)

# ============ CONFIG ============
MAX_CONCURRENT = 50
BATCH_SIZE = 100

# ============ CACHE ============
account_cache = {}
liked_cache = defaultdict(set)
token_cache = {}  # {uid: (token, timestamp)}
executor = ThreadPoolExecutor(max_workers=20)

# ============ TOKEN FUNCTIONS ============
def is_token_expired(token):
    """Check if JWT token is expired"""
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        exp = payload.get('exp', 0)
        if exp == 0:
            return False
        return time.time() > (exp - 300)  # 5 minutes buffer
    except:
        return False

def refresh_token_sync(uid, password):
    """Generate new token from password"""
    try:
        encoded_password = urllib.parse.quote(password)
        url = f"http://jwt-api-shappno.vercel.app/token?uid={uid}&password={encoded_password}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            token = data.get('token')
            if token:
                return token
    except Exception as e:
        print(f"Refresh failed for {uid}: {e}")
    return None

def get_valid_token(account):
    """Get valid token - auto refresh if expired"""
    uid = account['uid']
    
    # Check token cache first
    if uid in token_cache:
        token, timestamp = token_cache[uid]
        if not is_token_expired(token):
            return token
    
    # If account has token and not expired
    if account.get('is_token', False):
        token = account.get('token')
        if token and not is_token_expired(token):
            token_cache[uid] = (token, time.time())
            return token
    
    # If has password, refresh
    if account.get('password'):
        new_token = refresh_token_sync(uid, account['password'])
        if new_token:
            token_cache[uid] = (new_token, time.time())
            # Update account with new token
            account['token'] = new_token
            account['is_token'] = True
            return new_token
    
    return None

# ============ LOAD ACCOUNTS ============
def load_accounts():
    """Load accounts from shappno.txt"""
    cache_key = "accounts"
    if cache_key in account_cache:
        return account_cache[cache_key]
    
    accounts = []
    filename = "shappno.txt"
    
    if not os.path.exists(filename):
        print(f"❌ {filename} not found!")
        return []
    
    with open(filename, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' in line:
                parts = line.split(':', 1)
                uid = parts[0].strip()
                value = parts[1].strip()
                
                if uid and value:
                    # Check if it's a JWT token
                    if '.' in value and len(value) > 50:
                        accounts.append({
                            "uid": uid,
                            "token": value,
                            "is_token": True
                        })
                    else:
                        accounts.append({
                            "uid": uid,
                            "password": value,
                            "is_token": False
                        })
    
    account_cache[cache_key] = accounts
    print(f"✅ Loaded {len(accounts)} accounts from shappno.txt")
    return accounts

# ============ SAVE TOKENS TO FILE ============
def save_tokens_to_file(tokens):
    """Save tokens back to shappno.txt"""
    try:
        # Read existing lines
        lines = []
        with open("shappno.txt", 'r') as f:
            lines = f.readlines()
        
        # Update with new tokens
        new_lines = []
        updated_uids = set()
        
        for line in lines:
            line_stripped = line.strip()
            if line_stripped and ':' in line_stripped:
                uid = line_stripped.split(':', 1)[0].strip()
                if uid in tokens:
                    new_lines.append(f"{uid}:{tokens[uid]}\n")
                    updated_uids.add(uid)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        
        # Add new tokens
        for uid, token in tokens.items():
            if uid not in updated_uids:
                new_lines.append(f"{uid}:{token}\n")
        
        # Write back
        with open("shappno.txt", 'w') as f:
            f.writelines(new_lines)
        
        print(f"✅ Updated shappno.txt with {len(tokens)} tokens")
        return True
    except Exception as e:
        print(f"❌ Error saving: {e}")
        return False

# ============ ENCRYPTION ============
def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_message = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded_message)).decode('utf-8')

def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()

def enc(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return encrypt_message(message.SerializeToString())

def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except:
        return None

# ============ PLAYER INFO ============
def get_player_info_sync(encrypted_uid, server_name, token):
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB53"
    }

    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=8)
        return decode_protobuf(response.content)
    except:
        return None

# ============ SEND LIKE ============
def send_like_sync(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/x-www-form-urlencoded",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB53"
        }
        
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=5)
        return response.status
    except:
        return 500

def process_account_sync(target_uid, encrypted_uid, account, url):
    account_key = f"{account['uid']}:{target_uid}"
    if account_key in liked_cache[target_uid]:
        return 0, account['uid']
    
    # Get valid token (auto refresh if expired)
    token = get_valid_token(account)
    if not token:
        return 500, account['uid']
    
    status = send_like_sync(encrypted_uid, token, url)
    
    if status == 200:
        liked_cache[target_uid].add(account_key)
        return status, account['uid']
    
    return status, account['uid']

def send_all_likes_sync(target_uid, server_name, url):
    region = server_name
    protobuf_message = create_protobuf_message(target_uid, region)
    encrypted_uid = encrypt_message(protobuf_message)
    
    accounts = load_accounts()
    if not accounts:
        return {'success': 0, 'failed': 0, 'total': 0}
    
    # Get fresh accounts
    already_liked = liked_cache.get(target_uid, set())
    fresh_accounts = [acc for acc in accounts if f"{acc['uid']}:{target_uid}" not in already_liked]
    
    if not fresh_accounts:
        return {
            'success': 0,
            'failed': 0,
            'total': len(accounts),
            'already_liked': len(already_liked),
            'fresh_used': 0
        }
    
    random.shuffle(fresh_accounts)
    fresh_accounts = fresh_accounts[:2000]
    
    # Process in parallel
    results = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = []
        for acc in fresh_accounts:
            future = executor.submit(process_account_sync, target_uid, encrypted_uid, acc, url)
            futures.append(future)
        
        for future in futures:
            try:
                result = future.result(timeout=10)
                results.append(result)
            except:
                results.append((500, 'unknown'))
    
    successful = 0
    failed = 0
    new_tokens = {}
    
    for status, uid in results:
        if status == 200:
            successful += 1
            # Check if we have new token for this account
            for acc in accounts:
                if acc['uid'] == uid and acc.get('is_token', False):
                    new_tokens[uid] = acc['token']
        elif status != 0:
            failed += 1
    
    # Save new tokens to file
    if new_tokens:
        save_tokens_to_file(new_tokens)
    
    return {
        'success': successful,
        'failed': failed,
        'total': len(accounts),
        'already_liked': len(already_liked),
        'fresh_used': len(fresh_accounts)
    }

# ============ ROUTES ============
@app.route('/like', methods=['GET'])
def handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()

    if not uid or not server_name:
        return jsonify({"error": "UID and server_name required"}), 400

    valid_servers = ["IND", "BR", "US", "SAC", "NA", "BD", "RU"]
    if server_name not in valid_servers:
        return jsonify({"error": f"Invalid server. Use: {valid_servers}"}), 400

    accounts = load_accounts()
    if not accounts:
        return jsonify({"error": "No accounts found"}), 500
    
    # Get valid token for checking
    check_token = None
    for account in accounts[:3]:
        check_token = get_valid_token(account)
        if check_token:
            break
    
    if not check_token:
        return jsonify({"error": "No valid token found"}), 500
    
    encrypted_uid = enc(uid)

    try:
        before = get_player_info_sync(encrypted_uid, server_name, check_token)
        if before is None:
            return jsonify({"error": "Invalid UID or server", "status": 0}), 200
        
        before_data = json.loads(MessageToJson(before))
        before_like = int(before_data['AccountInfo'].get('Likes', 0))
    except:
        return jsonify({"error": "Data parsing failed", "status": 0}), 200
    
    if server_name == "IND":
        like_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        like_url = "https://client.us.freefiremobile.com/LikeProfile"
    else:
        like_url = "https://clientbp.ggpolarbear.com/LikeProfile"

    result = send_all_likes_sync(uid, server_name, like_url)

    try:
        after = get_player_info_sync(encrypted_uid, server_name, check_token)
        if after is None:
            return jsonify({"error": "Could not verify likes", "status": 0}), 200
        
        after_data = json.loads(MessageToJson(after))
        after_like = int(after_data['AccountInfo']['Likes'])
        player_id = int(after_data['AccountInfo']['UID'])
        player_name = str(after_data['AccountInfo']['PlayerNickname'])
        
        like_given = after_like - before_like
        status = 1 if like_given != 0 else 2

        return jsonify({
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": player_name,
            "UID": player_id,
            "status": status,
            "accounts_used": result.get('fresh_used', 0),
            "successful_likes": result.get('success', 0),
            "total_accounts": result.get('total', 0),
            "already_liked": result.get('already_liked', 0),
            "auto_refresh": "Active"
        })
    except Exception as e:
        return jsonify({"error": str(e), "status": 0}), 500

@app.route('/refresh', methods=['GET'])
def refresh_all_tokens():
    """Manually refresh all tokens"""
    accounts = load_accounts()
    refreshed = 0
    failed = 0
    
    for account in accounts:
        if account.get('password'):
            new_token = refresh_token_sync(account['uid'], account['password'])
            if new_token:
                account['token'] = new_token
                account['is_token'] = True
                token_cache[account['uid']] = (new_token, time.time())
                refreshed += 1
            else:
                failed += 1
    
    # Save to file
    if refreshed > 0:
        tokens = {acc['uid']: acc['token'] for acc in accounts if acc.get('is_token', False)}
        save_tokens_to_file(tokens)
    
    return jsonify({
        "status": "success",
        "refreshed": refreshed,
        "failed": failed,
        "total": len(accounts)
    })

@app.route('/health', methods=['GET'])
def health():
    accounts = load_accounts()
    token_count = sum(1 for acc in accounts if acc.get('is_token', False))
    return jsonify({
        "status": "healthy",
        "accounts_loaded": len(accounts),
        "tokens_available": token_count,
        "token_cache": len(token_cache),
        "auto_refresh": "Active (on each API call)"
    })

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "name": "SHAPPNO API",
        "version": "2.0",
        "features": [
            "No API key required",
            "Auto token refresh on each API call",
            "Reads from shappno.txt"
        ],
        "endpoints": {
            "/like": "Send likes (uid & server_name required)",
            "/refresh": "Manually refresh all tokens",
            "/health": "Check API status"
        },
        "credit": "@SHAPPNO"
    })

if __name__ == '__main__':
    print("🚀 SHAPPNO API Started!")
    print("✅ No API key required")
    print("✅ Auto refresh: Active (on each API call)")
    print("📁 Reading from: shappno.txt")
    app.run(host='0.0.0.0', port=5001, debug=False)