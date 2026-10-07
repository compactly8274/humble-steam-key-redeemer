import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from fuzzywuzzy import fuzz
import steam.webauth as wa
from steam.enums import EResult
import csv
import time
import pickle
from pwinput import pwinput
import os
import json
import re
import sys
import traceback
import unicodedata
import webbrowser
from base64 import b64encode
import atexit
import signal
from http.client import responses

#patch steam webauth for password feedback
wa.getpass = pwinput

# Patch steam webauth to report why a login was refused.
# Valve answers a rejected BeginAuthSessionViaCredentials with HTTP 200 and an empty
# {"response":{}} body, putting the real reason in the x-eresult header. The library
# ignores that header and indexes resp['response']['client_id'], so every refusal
# surfaced as an opaque KeyError instead of "wrong password".
ERESULT_CHECKED_METHODS = {"BeginAuthSessionViaCredentials"}
MAX_STEAM_PASSWORD_ATTEMPTS = 3
steam_refusals = {"invalid_password": 0}


def steam_api_error(method, eresult, message):
    try:
        result = EResult(int(eresult))
    except (TypeError, ValueError):
        result = None

    detail = message or (result.name if result else f"x-eresult={eresult}")

    if result == EResult.InvalidPassword:
        steam_refusals["invalid_password"] += 1
        if steam_refusals["invalid_password"] >= MAX_STEAM_PASSWORD_ATTEMPTS:
            # Steam reports a bad username with this same code, so stop looping and
            # let the user re-check the name rather than retyping the password forever.
            return wa.WebAuthException(
                f"Steam rejected these credentials {steam_refusals['invalid_password']} times. "
                "Check that 'Steam Username' is your account login name, not your email address."
            )
        # cli_login catches LoginIncorrect and re-prompts for the password.
        return wa.LoginIncorrect(
            "Steam rejected these credentials. Note that 'Steam Username' is your "
            "account login name, not the email address you sign in with."
        )
    if result == EResult.RateLimitExceeded:
        return wa.WebAuthException(
            "Steam is rate-limiting login attempts from this IP. Wait ~30 minutes and retry."
        )
    return wa.WebAuthException(f"Steam refused {method}: {detail}")


def send_api_request(data, steam_api_interface, steam_api_method, steam_api_version):
    url = wa.API_URL.format(steam_api_interface, steam_api_method, steam_api_version)

    if steam_api_method == "GetPasswordRSAPublicKey":  # GET, everything else is POST
        res = requests.get(url, timeout=10, headers=wa.API_HEADERS, params=data)
    else:
        res = requests.post(url, timeout=10, headers=wa.API_HEADERS, data=data)
    res.raise_for_status()

    try:
        body = res.json()
    except ValueError:
        raise wa.WebAuthException(
            f"Steam returned a non-JSON response for {steam_api_method} "
            f"(HTTP {res.status_code}): {res.text[:200]}"
        )

    # Only guard the credentials call -- the 2FA poll relies on an empty response
    # to signal "still waiting", and must keep raising KeyError for the library.
    if steam_api_method in ERESULT_CHECKED_METHODS and not body.get("response"):
        raise steam_api_error(
            steam_api_method,
            res.headers.get("x-eresult"),
            res.headers.get("x-error_message"),
        )

    return body


wa.WebAuth.send_api_request = staticmethod(send_api_request)

ERROR_LOG_FILE = "error.log"

# Per-app name lookups are rate-limited by Steam; see fetch_app_names.
APPDETAILS_MAX_LOOKUPS = 50
APPDETAILS_DELAY = 0.3

# Set in __main__. None while the module is merely imported, so log() stays quiet
# for anything that pulls these helpers in without wanting a log file.
LOG_STREAM = None
_output_files = {}


def log(message):
    """Append a timestamped line to error.log.

    The log is the run's only record once the console scrolls away, so it carries
    progress and decisions as well as failures. Writes are line-buffered and
    flushed: a run that dies mid-way still leaves everything up to that point.
    """
    if LOG_STREAM is None:
        return
    try:
        LOG_STREAM.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
        LOG_STREAM.flush()
    except Exception:
        # Logging must never be the thing that breaks a run.
        pass


def _open_output_file(filename, newline=None):
    if filename not in _output_files:
        _output_files[filename] = open(
            filename, "a", encoding="utf-8-sig", newline=newline
        )
    return _output_files[filename]


def close_output_files():
    for f in _output_files.values():
        try:
            f.close()
        except Exception:
            pass
    _output_files.clear()


# Humble endpoints
HUMBLE_LOGIN_PAGE = "https://www.humblebundle.com/login"
HUMBLE_KEYS_PAGE = "https://www.humblebundle.com/home/library"
HUMBLE_SUB_PAGE = "https://www.humblebundle.com/subscription/"

HUMBLE_LOGIN_API = "https://www.humblebundle.com/processlogin"
HUMBLE_REDEEM_API = "https://www.humblebundle.com/humbler/redeemkey"
HUMBLE_ORDERS_API = "https://www.humblebundle.com/api/v1/user/order"
HUMBLE_ORDER_DETAILS_API = "https://www.humblebundle.com/api/v1/order/"
HUMBLE_SUB_API = "https://www.humblebundle.com/api/v1/subscriptions/humble_monthly/subscription_products_with_gamekeys/"

HUMBLE_PAY_EARLY = "https://www.humblebundle.com/subscription/payearly"
HUMBLE_CHOOSE_CONTENT = "https://www.humblebundle.com/humbler/choosecontent"

# Steam endpoints
STEAM_KEYS_PAGE = "https://store.steampowered.com/account/registerkey"
STEAM_USERDATA_API = "https://store.steampowered.com/dynamicstore/userdata/"
STEAM_REDEEM_API = "https://store.steampowered.com/account/ajaxregisterkey/"
# ISteamApps/GetAppList was removed by Valve (404 "Method 'GetAppList' not found").
# IStoreService/GetAppList is the replacement but requires a Web API key, so the
# key-less endpoints below are used when no key is configured.
STEAM_APP_LIST_API = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
STEAM_APP_DETAILS_API = "https://store.steampowered.com/api/appdetails"
STEAM_APP_SEARCH_API = "https://steamcommunity.com/actions/SearchApps/"
STEAM_API_KEY_FILE = "steam_api_key.txt"

# May actually be able to do without these, but for now they're in.
# Per-game ownership prompts, restored with --interactive. Off by default: with
# the matching below, the prompt only ever fired on wrong candidates.
INTERACTIVE_MATCHING = False

headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def find_dict_keys(node, kv, parent=False):
    if isinstance(node, list):
        for i in node:
            for x in find_dict_keys(i, kv, parent):
               yield x
    elif isinstance(node, dict):
        if kv in node:
            if parent:
                yield node
            else:
                yield node[kv]
        for j in node.values():
            for x in find_dict_keys(j, kv, parent):
                yield x

getHumbleOrders = '''
var done = arguments[arguments.length - 1];
var list = '%optional%';
if (list){
    list = JSON.parse(list);
} else {
    list = [];
}
var getHumbleOrderDetails = async (list) => {
  const HUMBLE_ORDERS_API_URL = 'https://www.humblebundle.com/api/v1/user/order';
  const HUMBLE_ORDER_DETAILS_API = 'https://www.humblebundle.com/api/v1/order/';

  try {
    var orders = []
    if(list.length){
      orders = list.map(item => ({ gamekey: item }));
    } else {
      const response = await fetch(HUMBLE_ORDERS_API_URL);
      orders = await response.json();
    }
    const orderDetailsPromises = orders.map(async (order) => {
      const orderDetailsUrl = `${HUMBLE_ORDER_DETAILS_API}${order['gamekey']}?all_tpkds=true`;
      const orderDetailsResponse = await fetch(orderDetailsUrl);
      const orderDetails = await orderDetailsResponse.json();
      return orderDetails;
    });

    const orderDetailsArray = await Promise.all(orderDetailsPromises);
    return orderDetailsArray;
  } catch (error) {
    console.error('Error:', error);
    return [];
  }
};

getHumbleOrderDetails(list).then(r => {done(r)});
'''

fetch_cmd = '''
var done = arguments[arguments.length - 1];
var formData = new FormData();
const jsonData = JSON.parse(atob('{formData}'));

for (const key in jsonData) {{
    formData.append(key,jsonData[key])
}}

fetch("{url}", {{
  "headers": {{
    "csrf-prevention-token": "{csrf}"
    }},
  "body": formData,
  "method": "POST",
}}).then(r => {{ r.json().then( v=>{{done([r.status,v])}} ) }} );
'''

def perform_post(driver,url,payload):
    json_payload = b64encode(json.dumps(payload).encode('utf-8')).decode('ascii')
    csrf = driver.get_cookie('csrf_cookie')
    csrf = csrf['value'] if csrf is not None else ''
    if csrf is None:
        csrf = ''
    script = fetch_cmd.format(formData=json_payload,url=url,csrf=csrf)

    return driver.execute_async_script(fetch_cmd.format(formData=json_payload,url=url,csrf=csrf))

def process_quit(driver):
    def quit_on_exit(*args):
        driver.quit()

    atexit.register(quit_on_exit)
    signal.signal(signal.SIGTERM,quit_on_exit)
    signal.signal(signal.SIGINT,quit_on_exit)

def get_headless_driver():
    possibleDrivers = [(webdriver.Firefox,webdriver.FirefoxOptions),(webdriver.Chrome,webdriver.ChromeOptions)]
    driver = None

    exceptions = []
    for d,opt in possibleDrivers:
        try:
            options = opt()
            if d == webdriver.Chrome:
                options.add_argument("--headless=new")
            else:
                options.add_argument("-headless")
            driver = d(options=options)
            process_quit(driver) # make sure driver closes when we close
            return driver
        except WebDriverException as e:
            exceptions.append(('chrome:' if d == webdriver.Chrome else 'firefox:',e))
            continue
    cls()
    print("This script needs either Chrome or Firefox to be installed and the respective Web Driver for it to be configured (usually simplest is by placing it in the folder with the script)")
    print("")
    print("https://www.browserstack.com/guide/geckodriver-selenium-python")
    print("")
    print("Potential configuration hints:")
    for browser,exception in exceptions:
        print("")
        print(browser,exception.msg)

    try:
        time.sleep(30)
    except KeyboardInterrupt:
        pass
    sys.exit(1)

MODE_PROMPT = """Welcome to the Humble Exporter!
Which key export mode would you like to use?

[1] Auto-Redeem
[2] Export keys
[3] Humble Choice chooser
"""
def prompt_mode(order_details,humble_session):
    mode = None
    while mode not in ["1","2","3"]:
        print(MODE_PROMPT)
        mode = input("Choose 1, 2, or 3: ").strip()
        if mode in ["1","2","3"]:
            return mode
        print("Invalid mode")
    return mode


def valid_steam_key(key):
    # Steam keys are in the format of AAAAA-BBBBB-CCCCC
    if not isinstance(key, str):
        return False
    key_parts = key.split("-")
    return (
        len(key) == 17
        and len(key_parts) == 3
        and all(len(part) == 5 for part in key_parts)
    )


def try_recover_cookies(cookie_file, session):
    try:
        with open(cookie_file, "rb") as f:
            cookies = pickle.load(f)
        if type(session) is requests.Session:
            # handle Steam session
            session.cookies.update(cookies)
        else:
            # handle WebDriver
            for cookie in cookies:
                session.add_cookie(cookie)
        return True
    except Exception:
        return False


def export_cookies(cookie_file, session):
    try:
        if type(session) is requests.Session:
            # handle Steam session
            cookies = session.cookies
        else:
            # handle WebDriver
            cookies = session.get_cookies()
        with open(cookie_file, "wb") as f:
            pickle.dump(cookies, f)
        return True
    except Exception:
        return False

is_logged_in = '''
var done = arguments[arguments.length-1];

fetch("https://www.humblebundle.com/home/library").then(r => {done(!r.redirected)})
'''

def verify_logins_session(session):
    # Returns [humble_status, steam_status]
    if type(session) is requests.Session:
        loggedin = session.get(STEAM_KEYS_PAGE, allow_redirects=False).status_code not in (301,302)
        return [False,loggedin]
    else:
        return [session.execute_async_script(is_logged_in),False]

def do_login(driver,payload):
        auth,login_json = perform_post(driver,HUMBLE_LOGIN_API,payload)
        if auth not in (200,401):
            print(f"humblebundle.com has responded with an error (HTTP status code {auth}: {responses[auth]}).")
            time.sleep(30)
            sys.exit()
        return auth,login_json

def humble_login(driver):
    cls()
    driver.get(HUMBLE_LOGIN_PAGE)
    # Attempt to use saved session
    if try_recover_cookies(".humblecookies", driver) and verify_logins_session(driver)[0]:
        return True

    # Saved session didn't work
    while True:
        username = input("Humble Email: ")
        password = pwinput()

        payload = {
            "access_token": "",
            "access_token_provider_id": "",
            "goto": "/",
            "qs": "",
            "username": username,
            "password": password,
        }

        auth, login_json = do_login(driver, payload)

        if "errors" in login_json and "username" in login_json["errors"]:
            # Unknown email OR mismatched password
            print(login_json["errors"]["username"][0])
            continue

        while "humble_guard_required" in login_json or "two_factor_required" in login_json:
            # There may be differences for Humble's SMS 2FA, haven't tested.
            if "humble_guard_required" in login_json:
                humble_guard_code = input("Please enter the Humble security code: ")
                payload["guard"] = humble_guard_code.upper()
                # Humble security codes are case-sensitive via API, but luckily it's all uppercase!
                auth, login_json = do_login(driver, payload)

                if (
                    "user_terms_opt_in_data" in login_json
                    and login_json["user_terms_opt_in_data"]["needs_to_opt_in"]
                ):
                    # Nope, not messing with this.
                    print(
                        "There's been an update to the TOS, please sign in to Humble on your browser."
                    )
                    sys.exit(1)
            elif (
                "two_factor_required" in login_json and
                "errors" in login_json and
                "authy-input" in login_json["errors"]
            ):
                code = input("Please enter 2FA code: ")
                payload["code"] = code
                auth, login_json = do_login(driver, payload)
            elif "errors" in login_json:
                print("Unexpected login error detected.")
                print(login_json["errors"])
                sys.exit(1)

            if auth == 200:
                break

        export_cookies(".humblecookies", driver)
        return True


def steam_login():
    # Sign into Steam web

    # Attempt to use saved session
    r = requests.Session()
    if try_recover_cookies(".steamcookies", r) and verify_logins_session(r)[1]:
        return r

    # Saved state doesn't work, prompt user to sign in.
    s_username = input("Steam Username: ")
    user = wa.WebAuth(s_username)
    try:
        session = user.cli_login()
    except wa.WebAuthException as e:
        print("")
        print(f"Could not sign in to Steam: {e}")
        sys.exit(1)
    export_cookies(".steamcookies", session)
    return session


def redeem_humble_key(sess, tpk):
    # Keys need to be 'redeemed' on Humble first before the Humble API gives the user a Steam key.
    # This triggers that for a given Humble key entry
    payload = {"keytype": tpk["machine_name"], "key": tpk["gamekey"], "keyindex": tpk["keyindex"]}
    status,respjson = perform_post(sess, HUMBLE_REDEEM_API, payload)
    
    if status != 200 or "error_msg" in respjson or not respjson["success"]:
        print("Error redeeming key on Humble for " + key_title(tpk))
        if("error_msg" in respjson):
            print(respjson["error_msg"])
        return ""
    try:
        return respjson["key"]
    except:
        return respjson


def get_month_data(humble_session,month):
    # No real API for this, seems to just be served on the webpage.
    if type(humble_session) is not requests.Session:
        raise Exception("get_month_data needs a configured requests session")
    r = humble_session.get(HUMBLE_SUB_PAGE + month["product"]["choice_url"])

    data_indicator = f'<script id="webpack-monthly-product-data" type="application/json">'
    jsondata = r.text.split(data_indicator)[1].split("</script>")[0].strip()
    jsondata = json.loads(jsondata)
    return jsondata["contentChoiceOptions"]


def get_choices(humble_session,order_details):
    months = [
        month for month in order_details 
        if "choice_url" in month["product"] 
    ]

    # Oldest to Newest order
    months = sorted(months,key=lambda m: m["created"])
    request_session = requests.Session()
    for cookie in humble_session.get_cookies():
        # convert cookies to requests
        request_session.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'].replace('www.',''),path=cookie['path'])

    choices = []
    for month in months:
        if month["choices_remaining"] > 0 or month["product"].get("is_subs_v3_product",False): # subs v3 products don't advertise choices, need to get them exhaustively
            chosen_games = set(find_dict_keys(month["tpkd_dict"],"machine_name"))

            month["choice_data"] = get_month_data(request_session,month)
            if not month["choice_data"].get('canRedeemGames',True):
                month["available_choices"] = []
                continue

            v3 = not month["choice_data"].get("usesChoices",True)
            
            # Needed for choosing
            if v3:
                identifier = "initial"
                choice_options = month["choice_data"]["contentChoiceData"]["game_data"]
            else:
                identifier = "initial" if "initial" in month["choice_data"]["contentChoiceData"] else "initial-classic"
            
                if identifier not in month["choice_data"]["contentChoiceData"]:
                    for key in month["choice_data"]["contentChoiceData"].keys():
                        if "content_choices" in month["choice_data"]["contentChoiceData"][key]:
                            identifier = key

                choice_options = month["choice_data"]["contentChoiceData"][identifier]["content_choices"]

            # Exclude games that have already been chosen:
            month["available_choices"] = [
                    game[1]
                    for game in choice_options.items()
                    if set(find_dict_keys(game[1],"machine_name")).isdisjoint(chosen_games)
            ]
            
            month["parent_identifier"] = identifier
            if len(month["available_choices"]):
                yield month


def _redeem_steam(session, key, quiet=False):
    # Based on https://gist.github.com/snipplets/2156576c2754f8a4c9b43ccb674d5a5d
    if key == "":
        return 0
    cookies = session.cookies.get_dict()
    session_id = cookies.get("sessionid")
    if not session_id:
        print("Error: Steam sessionid cookie missing. Sign in again.")
        return 53
    try:
        r = session.post(STEAM_REDEEM_API, data={"product_key": key, "sessionid": session_id})
        blob = r.json()
    except ValueError as e:
        if not quiet:
            print(f"Error: Steam activation returned a non-JSON response: {e}")
        return 53

    if blob.get("success") == 1:
        for item in blob.get("purchase_receipt_info", {}).get("line_items", []):
            print("Redeemed " + item.get("line_item_description", "the product"))
        return 0
    else:
        error_code = blob.get("purchase_result_details")
        if error_code is None:
            # Sometimes purchase_result_details isn't there for some reason, try alt method
            receipt = blob.get("purchase_receipt_info")
            if receipt is not None:
                error_code = receipt.get("result_detail")
        error_code = error_code or 53

        if error_code == 14:
            error_message = (
                "The product code you've entered is not valid. Please double check to see if you've "
                "mistyped your key. I, L, and 1 can look alike, as can V and Y, and 0 and O. "
            )
        elif error_code == 15:
            error_message = (
                "The product code you've entered has already been activated by a different Steam account. "
                "This code cannot be used again. Please contact the retailer or online seller where the "
                "code was purchased for assistance. "
            )
        elif error_code == 53:
            error_message = (
                "There have been too many recent activation attempts from this account or Internet "
                "address. Please wait and try your product code again later. "
            )
        elif error_code == 13:
            error_message = (
                "Sorry, but this product is not available for purchase in this country. Your product key "
                "has not been redeemed. "
            )
        elif error_code == 9:
            error_message = (
                "This Steam account already owns the product(s) contained in this offer. To access them, "
                "visit your library in the Steam client. "
            )
        elif error_code == 24:
            error_message = (
                "The product code you've entered requires ownership of another product before "
                "activation.\n\nIf you are trying to activate an expansion pack or downloadable content, "
                "please first activate the original game, then activate this additional content. "
            )
        elif error_code == 36:
            error_message = (
                "The product code you have entered requires that you first play this game on the "
                "PlayStation®3 system before it can be registered.\n\nPlease:\n\n- Start this game on "
                "your PlayStation®3 system\n\n- Link your Steam account to your PlayStation®3 Network "
                "account\n\n- Connect to Steam while playing this game on the PlayStation®3 system\n\n- "
                "Register this product code through Steam. "
            )
        elif error_code == 50:
            error_message = (
                "The code you have entered is from a Steam Gift Card or Steam Wallet Code. Browse here: "
                "https://store.steampowered.com/account/redeemwalletcode to redeem it. "
            )
        else:
            error_message = (
                "An unexpected error has occurred.  Your product code has not been redeemed.  Please wait "
                "30 minutes and try redeeming the code again.  If the problem persists, please contact <a "
                'href="https://help.steampowered.com/en/wizard/HelpWithCDKey">Steam Support</a> for '
                "further assistance. "
            )
        if error_code != 53 or not quiet:
            print(error_message)
        return error_code


# write_key writes through _open_output_file so close_output_files() actually
# closes these; it previously kept a second, separate dict.


def write_key(code, key):
    filename = "redeemed.csv"
    if code == 15 or code == 9:
        filename = "already_owned.csv"
    elif code != 0:
        filename = "errored.csv"

    handle = _open_output_file(filename, newline="")
    writer = csv.writer(handle)
    gamekey = key.get("gamekey")
    human_name = key_title(key)
    redeemed_key_val = key.get("redeemed_key_val")
    writer.writerow([gamekey, human_name, redeemed_key_val])
    handle.flush()
    log(f"key result: code={code} file={filename} name={human_name!r}")


def prompt_skipped(skipped_games):
    user_filtered = []
    with open("skipped.txt", "w", encoding="utf-8-sig") as file:
        for skipped_game in skipped_games.keys():
            file.write(skipped_game + "\n")

    print(
        f"Inside skipped.txt is a list of {len(skipped_games)} games that we think you already own, but aren't "
        f"completely sure "
    )
    try:
        input(
            "Feel free to REMOVE from that list any games that you would like to try anyways, and when done press "
            "Enter to confirm. "
        )
    except SyntaxError:
        pass
    if os.path.exists("skipped.txt"):
        with open("skipped.txt", "r", encoding="utf-8-sig") as file:
            user_filtered = [line.strip() for line in file]
        os.remove("skipped.txt")
    # Choose only the games that appear to be missing from user's skipped.txt file
    user_requested = [
        skip_game
        for skip_name, skip_game in skipped_games.items()
        if skip_name not in user_filtered
    ]
    return user_requested


def prompt_yes_no(question):
    ans = None
    answers = ["y","n"]
    while ans not in answers:
        prompt = f"{question} [{'/'.join(answers)}] "

        ans = input(prompt).strip().lower()
        if ans not in answers:
            print(f"{ans} is not a valid answer")
            continue
        else:
            return True if ans == "y" else False

def load_steam_api_key():
    # Optional. With a key we can pull Steam's whole catalogue in a few requests;
    # without one we fall back to per-title searches.
    if not os.path.exists(STEAM_API_KEY_FILE):
        return None
    try:
        with open(STEAM_API_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        print(f"Warning: couldn't read {STEAM_API_KEY_FILE}; falling back to per-title lookups.")
        return None


def steam_json(steam_session, url, what, **kwargs):
    # Steam hands back HTML error pages and empty bodies often enough that a bare
    # .json() turns every hiccup into an unreadable JSONDecodeError traceback.
    try:
        resp = steam_session.get(url, **kwargs)
    except requests.RequestException as e:
        print(f"Error: request for {what} failed: {e}")
        return None
    try:
        return resp.json()
    except ValueError:
        preview = " ".join(resp.text[:200].split())
        print(f"Error: {what} was not JSON (HTTP {resp.status_code}). Body: {preview}")
        return None


def fetch_steam_catalog(steam_session, api_key):
    # Paginated full catalogue via IStoreService/GetAppList. Returns appid -> name.
    params = {
        "key": api_key,
        "max_results": 50000,
        "last_appid": 0,
        "include_games": 1,
        "include_dlc": 1,
        "include_software": 1,
        "include_hardware": 1,
    }

    print("Fetching the Steam catalogue (this takes a few moments)...")
    catalog = {}
    while True:
        body = steam_json(steam_session, STEAM_APP_LIST_API, "the Steam app list", params=params)
        if body is None:
            print("Could not fetch the Steam catalogue; falling back to per-title lookups.")
            print(f"If your key is wrong or expired, fix or delete {STEAM_API_KEY_FILE}.")
            return None

        response = body.get("response", {})
        page = response.get("apps", [])
        catalog.update({app["appid"]: app["name"] for app in page})

        if not page or not response.get("have_more_results"):
            break
        params["last_appid"] = response.get("last_appid", params["last_appid"])

    print(f"Fetched {len(catalog)} apps from the Steam catalogue.")
    log(f"catalogue fetched: {len(catalog)} apps")
    return catalog


def fetch_app_names(steam_session, app_ids):
    """Best-effort names for owned apps the catalogue omits (store-delisted ones).

    store/api/appdetails is one request per app and throttled hard -- a few hundred
    rapid calls earn a 403 for the whole IP, which then also blocks the title search
    the key-less path depends on. These names only sharpen title matching, so this
    stays small, paces itself, and gives up quietly rather than stalling the run.
    """
    if not app_ids:
        return {}

    if len(app_ids) > APPDETAILS_MAX_LOOKUPS:
        message = (
            f"{len(app_ids)} owned apps are missing from the Steam catalogue "
            f"(usually store-delisted). Skipping per-app name lookups: more than "
            f"{APPDETAILS_MAX_LOOKUPS} would get this IP throttled. Exact app-id "
            f"matching is unaffected."
        )
        print(message)
        log(message)
        return {}

    names = {}
    for appid in app_ids:
        try:
            resp = steam_session.get(
                STEAM_APP_DETAILS_API, params={"appids": appid}, timeout=20
            )
        except requests.RequestException as e:
            log(f"appdetails {appid}: request failed: {e}")
            continue

        if resp.status_code in (403, 429):
            message = (
                f"Steam is throttling app-detail lookups (HTTP {resp.status_code}); "
                f"resolved {len(names)} of {len(app_ids)} names before stopping."
            )
            print(message)
            log(message)
            break

        try:
            body = resp.json()
        except ValueError:
            log(f"appdetails {appid}: non-JSON response (HTTP {resp.status_code})")
            continue

        # appdetails answers a bare `null` for some app ids, so body itself can be
        # None. Indexing it directly is what raised
        # "'NoneType' object has no attribute 'get'" mid-run.
        entry = (body or {}).get(str(appid)) or {}
        name = (entry.get("data") or {}).get("name") if entry.get("success") else None
        if name:
            names[appid] = name

        time.sleep(APPDETAILS_DELAY)

    log(f"appdetails: resolved {len(names)} of {len(app_ids)} missing names")
    return names


def search_owned_candidates(steam_session, game_name, owned_app_ids, cache):
    """Key-less ownership lookup: ask Steam's search for this title, keep what we own.

    Returns appid -> name for matching apps the user already owns, in the same shape
    the full catalogue would have given, so classify_ownership works unchanged.
    """
    term = game_name.strip()
    if term in cache:
        return cache[term]

    if cache:
        time.sleep(0.2)  # hundreds of titles in a run; don't get the IP throttled

    results = steam_json(
        steam_session,
        STEAM_APP_SEARCH_API + requests.utils.quote(term),
        f'the Steam search for "{term}"',
        timeout=20,
    )

    candidates = {}
    for app in results or []:
        try:
            appid = int(app["appid"])
        except (KeyError, TypeError, ValueError):
            continue
        if appid in owned_app_ids:
            candidates[appid] = app.get("name", "")

    cache[term] = candidates
    return candidates


def get_owned_apps(steam_session):
    """Return (owned_app_ids, owned_app_details).

    owned_app_details is the appid -> name map for everything owned when a Web API
    key is configured, or None when callers must fall back to per-title searches.
    """
    owned_content = steam_json(steam_session, STEAM_USERDATA_API, "your Steam user data")
    if owned_content is None or "rgOwnedApps" not in owned_content:
        print("Could not read your owned Steam apps. Delete .steamcookies and sign in again.")
        print("Stopping rather than attempting every key: Steam allows only ~10 failed")
        print("keys per hour, and keys you already own count as failures.")
        sys.exit(1)

    # Only app IDs: rgOwnedPackages holds package IDs, a different namespace from
    # the Steam app IDs Humble reports, so comparing against them is meaningless.
    owned_app_ids = set(owned_content["rgOwnedApps"])

    api_key = load_steam_api_key()
    if not api_key:
        print("No Steam Web API key found; matching titles individually instead.")
        print("For faster matching, put a key from https://steamcommunity.com/dev/apikey")
        print(f"into {STEAM_API_KEY_FILE}.")
        return owned_app_ids, None

    catalog = fetch_steam_catalog(steam_session, api_key)
    if catalog is None:
        return owned_app_ids, None

    owned_app_details = {
        appid: catalog[appid] for appid in owned_app_ids if appid in catalog
    }

    missing = [appid for appid in owned_app_ids if appid not in catalog]
    if missing:
        owned_app_details.update(fetch_app_names(steam_session, missing))

    unresolved = len(owned_app_ids) - len(owned_app_details)
    if unresolved:
        print(f"Warning: couldn't resolve names for {unresolved} of your owned apps.")
    log(
        f"ownership data: {len(owned_app_ids)} owned app ids, "
        f"{len(owned_app_details)} with names, {unresolved} unresolved"
    )

    return owned_app_ids, owned_app_details

# Ownership verdicts. Precision matters more than recall here: a false "owned"
# silently drops a key the user does not have, while a false "not owned" costs one
# attempt against Steam's ~10-failures-per-hour limit and is recorded in
# already_owned.csv so later runs skip it.
OWNED = "owned"
UNCERTAIN = "uncertain"
NOT_OWNED = "not-owned"

AUTO_OWNED_SCORE = 95
NEAR_MISS_SCORE = 80
OWNERSHIP_REPORT = "ownership_report.csv"

# Qualifiers that denote the same game repackaged, so owning either side counts.
EDITION_QUALIFIERS = (
    "game of the year edition", "game of the year", "goty edition", "goty",
    "complete edition", "complete pack", "definitive edition", "deluxe edition",
    "enhanced edition", "ultimate edition", "gold edition", "premium edition",
    "special edition", "anniversary edition", "remastered edition", "remastered",
    "redux", "directors cut", "the final cut", "legendary edition",
)

# Normalised once at import rather than per comparison.
EDITION_QUALIFIER_NORMS = tuple(
    " ".join(q.lower().replace("'", "").split()) for q in EDITION_QUALIFIERS
)

# Sequels are routinely written as roman numerals on one store and digits on the
# other. "i" is left out deliberately -- it collides with the pronoun.
ROMAN_NUMERALS = {
    "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12", "xiii": "13",
}


def key_title(key):
    """Display name for a Humble entry.

    Entries found under "steam_app_id" do not all carry a "human_name" -- one
    without it raised KeyError partway through a 1900-key run -- so fall back
    through the other names Humble uses before giving up.
    """
    human_name = key.get("human_name")
    if human_name:
        return str(human_name)

    # The remaining fields are slugs, and Humble suffixes Steam ones with _steam
    # ("stardew_valley_steam"); left in place it defeats the title comparison.
    for field in ("machine_name", "display_item_machine_name"):
        value = key.get(field)
        if value:
            slug = str(value)
            if slug.endswith("_steam"):
                slug = slug[: -len("_steam")]
            return slug.replace("_", " ")
    gamekey = key.get("gamekey")
    appid = key.get("steam_app_id")
    return f"<unnamed key gamekey={gamekey} steam_app_id={appid}>"


def normalize_title(title):
    """Reduce a store title to a comparable form, keeping what distinguishes games."""
    if not title:
        return ""
    text = str(title)
    # Strip trademark glyphs before NFKD, which would expand U+2122 into "TM".
    text = re.sub(r"[™®©]", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"\((?:[^)]*\b(?:steam|pc|key)\b[^)]*)\)", " ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[‐-―]", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(ROMAN_NUMERALS.get(t, t) for t in text.split()).strip()


def series_numbers(normalized):
    """Numeric tokens in order: 'portal' -> [], 'portal 2' -> ['2']."""
    return [token for token in normalized.split() if token.isdigit()]


class OwnershipIndex:
    """Owned titles, normalised once.

    classify_ownership runs per Humble key. Normalising every owned title inside
    that call meant thousands of redundant normalisations per key -- roughly
    400ms per key against a 2500-game library, or 12 minutes over a full run.
    Building this once drops exact matches to a dict lookup.
    """

    __slots__ = ("names", "by_norm", "entries")

    def __init__(self, owned_app_details):
        self.names = owned_app_details or {}
        self.by_norm = {}
        self.entries = []
        for appid, appname in self.names.items():
            norm = normalize_title(appname)
            if not norm:
                continue
            self.by_norm.setdefault(norm, appid)
            self.entries.append((norm, series_numbers(norm), len(norm), appid))

    def __bool__(self):
        return bool(self.entries)


def build_ownership_index(owned_app_details):
    return OwnershipIndex(owned_app_details)


def classify_ownership(index, game, interactive=False):
    """Decide whether the user already owns this Humble title.

    Returns (verdict, confidence, appid).

    The original implementation scored candidates with fuzz.token_set_ratio,
    which returns 100 whenever one title merely contains the other. Every
    sequel, expansion and edition therefore looked like a match: "Arma 2" scored
    100 against "Arma 2: Operation Arrowhead", so the run stopped to ask about
    each one, and non-interactively a score of 38 still counted as owned.
    """
    if not isinstance(index, OwnershipIndex):
        # Convenience for callers holding a plain appid -> name mapping.
        index = OwnershipIndex(index)

    humble_norm = normalize_title(key_title(game))
    if not humble_norm or not index:
        return NOT_OWNED, 0, None

    # Exact title, then the same title with an edition qualifier on either side.
    # All dict lookups, so none of this touches the candidate list.
    appid = index.by_norm.get(humble_norm)
    if appid is not None:
        return OWNED, 100, appid

    for qualifier in EDITION_QUALIFIER_NORMS:
        appid = index.by_norm.get(f"{humble_norm} {qualifier}")
        if appid is not None:
            return OWNED, 100, appid
        suffix = " " + qualifier
        if humble_norm.endswith(suffix):
            appid = index.by_norm.get(humble_norm[: -len(suffix)])
            if appid is not None:
                return OWNED, 100, appid

    humble_nums = series_numbers(humble_norm)
    humble_len = len(humble_norm)
    near_misses = []
    for owned_norm, owned_nums, owned_len, appid in index.entries:
        # A differing set of numbers means a different entry in the series.
        # Without this, "Portal" vs "Portal 2" scores 86 and "Dishonored" vs
        # "Dishonored 2" scores 91 -- indistinguishable from a real match.
        if owned_nums != humble_nums:
            continue
        # SequenceMatcher's ratio cannot exceed 2*min_len/(len_a+len_b), so skip
        # pairs that could never reach the band instead of scoring them.
        if 200 * min(humble_len, owned_len) < NEAR_MISS_SCORE * (humble_len + owned_len):
            continue
        score = fuzz.token_sort_ratio(owned_norm, humble_norm)
        if score >= NEAR_MISS_SCORE:
            near_misses.append((score, appid))

    if not near_misses:
        return NOT_OWNED, 0, None

    score, appid = max(near_misses, key=lambda match: match[0])
    if score >= AUTO_OWNED_SCORE:
        return OWNED, score, appid

    if interactive:
        cls()
        print(f'Humble key: "{key_title(game)}"')
        print("Similar games you already own on Steam:")
        for near_score, near_appid in sorted(near_misses, reverse=True):
            print(f"     {index.names.get(near_appid, near_appid)}: {near_score}")
        if prompt_yes_no("Do you already own this game?"):
            return OWNED, score, appid
        return NOT_OWNED, score, None

    return UNCERTAIN, score, appid


def write_ownership_report(rows):
    """Record every ownership decision, so skips can be reviewed after a run."""
    try:
        with open(OWNERSHIP_REPORT, "w", encoding="utf-8-sig") as f:
            f.write("verdict,humble_name,matched_steam_name,appid,confidence\n")
            for verdict, humble_name, steam_name, appid, score in rows:
                cells = [
                    verdict,
                    str(humble_name).replace(",", "."),
                    str(steam_name or "").replace(",", "."),
                    str(appid if appid is not None else ""),
                    str(score),
                ]
                f.write(",".join(cells) + "\n")
    except OSError as e:
        print(f"Warning: couldn't write {OWNERSHIP_REPORT}: {e}")


def redeem_steam_keys(humble_session, humble_keys):
    session = steam_login()

    print("Successfully signed in on Steam.")
    print("Getting your owned content to avoid attempting to register keys already owned...")

    # Query owned App IDs according to Steam
    owned_app_ids, owned_app_details = get_owned_apps(session)

    noted_keys = [key for key in humble_keys if key["steam_app_id"] not in owned_app_ids]
    skipped_games = {}
    unownedgames = []
    report_rows = []
    uncertain_count = 0

    # Some Steam keys come back with no Steam AppID from Humble
    # So we do our best to look up from AppIDs (no packages, because can't find an API for it)

    search_cache = {}
    # Normalising the catalogue once instead of per key: with a 2500-game library
    # the per-key version cost ~400ms each, about 12 minutes over a full run.
    catalog_index = (
        build_ownership_index(owned_app_details) if owned_app_details is not None else None
    )

    for game in noted_keys:
        title = key_title(game)
        if owned_app_details is None:
            # No API key: look this one title up instead of scanning a full catalogue.
            candidates = search_owned_candidates(
                session, title, owned_app_ids, search_cache
            )
            index = build_ownership_index(candidates)
        else:
            candidates = owned_app_details
            index = catalog_index

        verdict, score, appid = classify_ownership(index, game, INTERACTIVE_MATCHING)
        matched_name = candidates.get(appid) if appid is not None else None
        report_rows.append((verdict, title, matched_name, appid, score))

        if verdict == OWNED and appid is not None:
            skipped_games[title.strip()] = game
        else:
            # Uncertain titles are attempted: Steam is the authoritative check, and it
            # reports "already owned" into already_owned.csv for later runs to filter.
            if verdict == UNCERTAIN:
                uncertain_count += 1
            unownedgames.append(game)

    write_ownership_report(report_rows)
    log(
        f"ownership decisions: {len(skipped_games)} owned (skipped), "
        f"{uncertain_count} uncertain (attempted), "
        f"{len(unownedgames)} to attempt of {len(noted_keys)} considered"
    )

    print(
        "Filtered out game keys that you already own on Steam; {} keys unowned.".format(
            len(unownedgames)
        )
    )
    if uncertain_count:
        print(
            f"{uncertain_count} were close matches that could not be decided from the "
            f"title alone; attempting them. See {OWNERSHIP_REPORT}."
        )

    if len(skipped_games):
        if INTERACTIVE_MATCHING:
            # Skipped games uncertain to be owned by user. Let user choose
            unownedgames = unownedgames + prompt_skipped(skipped_games)
        else:
            print(
                f"Skipped {len(skipped_games)} keys matched to games you already own "
                f"(listed as '{OWNED}' in {OWNERSHIP_REPORT})."
            )
        print("{} keys will be attempted.".format(len(unownedgames)))
        # Preserve original order
        unownedgames = sorted(unownedgames,key=lambda g: humble_keys.index(g))
    
    redeemed = []

    for key in unownedgames:
        title = key_title(key)
        print(title)

        if title in redeemed or (key["steam_app_id"] != None and key["steam_app_id"] in redeemed):
            # We've bumped into a repeat of the same game!
            write_key(9,key)
            continue
        else:
            if key["steam_app_id"] != None:
                redeemed.append(key["steam_app_id"])
            redeemed.append(title)

        if "redeemed_key_val" not in key:
            # This key is unredeemed via Humble, trigger redemption process.
            redeemed_key = redeem_humble_key(humble_session, key)
            key["redeemed_key_val"] = redeemed_key
            # Worth noting this will only persist for this loop -- does not get saved to unownedgames' obj

        if not valid_steam_key(key["redeemed_key_val"]):
            # Most likely humble gift link
            write_key(1, key)
            continue

        code = _redeem_steam(session, key["redeemed_key_val"])
        animation = "|/-\\"
        seconds = 0
        while code == 53:
            """NOTE
            Steam seems to limit to about 50 keys/hr -- even if all 50 keys are legitimate *sigh*
            Even worse: 10 *failed* keys/hr
            Duplication counts towards Steam's _failure rate limit_,
            hence why we've worked so hard above to figure out what we already own
            """
            current_animation = animation[seconds % len(animation)]
            print(
                f"Waiting for rate limit to go away (takes an hour after first key insert) {current_animation}",
                end="\r",
            )
            time.sleep(1)
            seconds = seconds + 1
            if seconds % 60 == 0:
                # Try again every 60 seconds
                code = _redeem_steam(session, key["redeemed_key_val"], quiet=True)

        write_key(code, key)


def export_mode(humble_session,order_details):
    cls()

    export_key_headers = ['human_name','redeemed_key_val','is_gift','key_type_human_name','is_expired','steam_ownership']

    steam_session = None
    reveal_unrevealed = False
    confirm_reveal = False

    owned_app_ids = None
    owned_app_details = {}

    keys = []
    
    print("Please configure your export:")
    export_steam_only = prompt_yes_no("Export only Steam keys?")
    export_revealed = prompt_yes_no("Export revealed keys?")
    export_unrevealed = prompt_yes_no("Export unrevealed keys?")
    if(not export_revealed and not export_unrevealed):
        print("That leaves 0 keys...")
        sys.exit()
    if(export_unrevealed):
        reveal_unrevealed = prompt_yes_no("Reveal all unrevealed keys? (This will remove your ability to claim gift links on these)")
        if(reveal_unrevealed):
            extra = "Steam " if export_steam_only else ""
            confirm_reveal = prompt_yes_no(f"Please CONFIRM that you would like ALL {extra}keys on Humble to be revealed, this can't be undone.")
    steam_config = prompt_yes_no("Would you like to sign into Steam to detect ownership on the export data?")
    
    if(steam_config):
        steam_session = steam_login()
        if(verify_logins_session(steam_session)[1]):
            owned_app_ids, owned_app_details = get_owned_apps(steam_session)
            if owned_app_details is None:
                # No Web API key, so there is no catalogue to match names against.
                # Ownership is still exact on the app IDs Humble supplies.
                owned_app_details = {}
    
    export_index = build_ownership_index(owned_app_details)

    desired_keys = "steam_app_id" if export_steam_only else "key_type_human_name"
    keylist = list(find_dict_keys(order_details,desired_keys,True))

    for idx,tpk in enumerate(keylist):
        revealed = "redeemed_key_val" in tpk
        export = (export_revealed and revealed) or (export_unrevealed and not revealed)

        if(export):
            if(export_unrevealed and confirm_reveal):
                # Redeem key if user requests all keys to be revealed
                tpk["redeemed_key_val"] = redeem_humble_key(humble_session,tpk)
            
            if(owned_app_ids != None and "steam_app_id" in tpk):
                # User requested Steam Ownership info
                owned = tpk["steam_app_id"] in owned_app_ids
                if(not owned):
                    # Do a search to see if user owns it
                    verdict, _score, _appid = classify_ownership(export_index, tpk)
                    owned = verdict == OWNED
                tpk["steam_ownership"] = owned
            
            keys.append(tpk)
    
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"humble_export_{ts}.csv"
    with open(filename, 'w', encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=export_key_headers, extrasaction="ignore")
        writer.writeheader()
        for key in keys:
            writer.writerow({col: key.get(col, "") for col in export_key_headers})

    print(f"Exported to {filename}")


def choose_games(humble_session,choice_month_name,identifier,chosen):
    for choice in chosen:
        display_name = choice["display_item_machine_name"]
        if "tpkds" not in choice:
            webbrowser.open(f"{HUMBLE_SUB_PAGE}{choice_month_name}/{display_name}")
        else:
            payload = {
                "gamekey":choice["tpkds"][0]["gamekey"],
                "parent_identifier":identifier,
                "chosen_identifiers[]":display_name,
                "is_multikey_and_from_choice_modal":"false"
            }
            status,res = perform_post(humble_session,HUMBLE_CHOOSE_CONTENT,payload)
            if not ("success" in res or not res["success"]):
                print("Error choosing " + choice["title"])
                print(res)
            else:
                print("Chose game " + choice["title"])


def humble_chooser_mode(humble_session,order_details):
    try_redeem_keys = []
    months = get_choices(humble_session,order_details)
    first = True
    for month in months:
        redeem_all = None
        if(first):
            redeem_keys = prompt_yes_no("Would you like to auto-redeem these keys after? (Will require Steam login)")
            first = False
        
        ready = False
        while not ready:
            cls()
            if month["choice_data"]["usesChoices"]:
                remaining = month["choices_remaining"]
                print()
                print(month["product"]["human_name"])
                print(f"Choices remaining: {remaining}")
            else:
                remaining = len(month["available_choices"])
            print("Available Games:\n")
            choices = month["available_choices"]
            for idx,choice in enumerate(choices):
                title = choice["title"]
                rating_text = ""
                if("review_text" in choice["user_rating"] and "steam_percent|decimal" in choice["user_rating"]):
                    rating = choice["user_rating"]["review_text"].replace('_',' ')
                    percentage = str(int(choice["user_rating"]["steam_percent|decimal"]*100)) + "%"
                    rating_text = f" - {rating}({percentage})"
                exception = ""
                if "tpkds" not in choice:
                    # These are weird cases that should be handled by Humble.
                    exception = " (Must be redeemed through Humble directly)"
                print(f"{idx+1}. {title}{rating_text}{exception}")
            if(redeem_all == None and remaining == len(choices)):
                redeem_all = prompt_yes_no("Would you like to redeem all?")
            else:
                redeem_all = False
            
            if(redeem_all):
                user_input = [str(i+1) for i in range(0,len(choices))]
            else:
                if(redeem_keys):
                    auto_redeem_note = "(We'll auto-redeem any keys activated via the webpage if you continue after!)"
                else:
                    auto_redeem_note = ""
                print("\nOPTIONS:")
                print("To choose games, list the indexes separated by commas (e.g. '1' or '1,2,3')")
                print(f"Or type just 'link' to go to the webpage for this month {auto_redeem_note}")
                print("Or just press Enter to move on.")

                user_input = [uinput.strip() for uinput in input().split(',') if uinput.strip() != ""]

            if(len(user_input) == 0):
                ready = True
            elif(user_input[0].lower() == 'link'):
                webbrowser.open(HUMBLE_SUB_PAGE + month["product"]["choice_url"])
                if redeem_keys:
                    # May have redeemed keys on the webpage.
                    try_redeem_keys.append(month["gamekey"])
            else:
                invalid_option = lambda option: (
                    not option.isnumeric()
                    or option == "0" 
                    or int(option) > len(choices)
                )
                invalid = [option for option in user_input if invalid_option(option)]

                if(len(invalid) > 0):
                    print("Error interpreting options: " + ','.join(invalid))
                    time.sleep(2)
                else:
                    user_input = set(int(opt) for opt in user_input) # Uniques
                    chosen = [choice for idx,choice in enumerate(choices) if idx+1 in user_input]
                    # This weird enumeration is to keep it in original display order

                    if len(chosen) > remaining:
                        print(f"Too many games chosen, you have only {remaining} choices left")
                        time.sleep(2)
                    else:
                        print("\nGames selected:")
                        for choice in chosen:
                            print(choice["title"])
                        confirmed = prompt_yes_no("Please type 'y' to confirm your selection")
                        if confirmed:
                            choice_month_name = month["product"]["choice_url"]
                            identifier = month["parent_identifier"]
                            choose_games(humble_session,choice_month_name,identifier,chosen)
                            if redeem_keys:
                                try_redeem_keys.append(month["gamekey"])
                            ready = True
    if(first):
        print("No Humble Choices need choosing! Look at you all up-to-date!")
    else:
        print("No more unchosen Humble Choices")
        if(redeem_keys and len(try_redeem_keys) > 0):
            print("Redeeming keys now!")
            updated_monthlies = humble_session.execute_async_script(getHumbleOrders.replace('%optional%',json.dumps(try_redeem_keys)))
            chosen_keys = list(find_dict_keys(updated_monthlies,"steam_app_id",True))
            redeem_steam_keys(humble_session,chosen_keys)

def cls():
    os.system('cls' if os.name=='nt' else 'clear')
    print_main_header()

def print_main_header():
    print("-=FailSpy's Humble Bundle Helper!=-")
    print("--------------------------------------")


def main():
    global INTERACTIVE_MATCHING
    INTERACTIVE_MATCHING = "--interactive" in sys.argv
    if INTERACTIVE_MATCHING:
        print("Interactive matching on: you'll be asked about ambiguous titles.")

    # Create a consistent session for Humble API use
    driver = get_headless_driver()
    humble_login(driver)
    print("Successfully signed in on Humble.")

    print(f"Getting order details, please wait")

    order_details = driver.execute_async_script(getHumbleOrders.replace('%optional%',''))

    desired_mode = prompt_mode(order_details,driver)
    if(desired_mode == "2"):
        export_mode(driver,order_details)
        return
    if(desired_mode == "3"):
        humble_chooser_mode(driver,order_details)
        return

    # Auto-Redeem mode
    cls()
    unrevealed_keys = []
    revealed_keys = []
    steam_keys = list(find_dict_keys(order_details,"steam_app_id",True))

    filters = ["errored.csv", "already_owned.csv", "redeemed.csv"]
    original_length = len(steam_keys)
    for filter_file in filters:
        try:
            with open(filter_file, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header if present
                seen = set(row[0].strip() for row in reader if row)
            steam_keys = [key for key in steam_keys if key.get("gamekey") not in seen]
        except FileNotFoundError:
            pass
    if len(steam_keys) != original_length:
        print("Filtered {} keys from previous runs".format(original_length - len(steam_keys)))

    for key in steam_keys:
        if "redeemed_key_val" in key:
            revealed_keys.append(key)
        else:
            # Has not been revealed via Humble yet
            unrevealed_keys.append(key)

    print(
        f"{len(steam_keys)} Steam keys total -- {len(revealed_keys)} revealed, {len(unrevealed_keys)} unrevealed"
    )

    will_reveal_keys = prompt_yes_no("Would you like to redeem on Humble as-yet un-revealed Steam keys?"
                                " (Revealing keys removes your ability to generate gift links for them)")
    if will_reveal_keys:
        try_already_revealed = prompt_yes_no("Would you like to attempt redeeming already-revealed keys as well?")
        # User has chosen to either redeem all keys or just the 'unrevealed' ones.
        redeem_steam_keys(driver, steam_keys if try_already_revealed else unrevealed_keys)
    else:
        # User has excluded unrevealed keys.
        redeem_steam_keys(driver, revealed_keys)

    # Cleanup
    close_output_files()


if __name__=="__main__":
    console_stderr = sys.stderr
    # Line-buffered: a hard crash still leaves every line already written on disk.
    LOG_STREAM = open(ERROR_LOG_FILE, "a", buffering=1, encoding="utf-8")
    sys.stderr = LOG_STREAM

    log("=" * 70)
    log(f"run started -- python {sys.version.split()[0]}, args {sys.argv[1:] or 'none'}")

    exit_code = 0
    try:
        main()
        log("run finished normally")
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 0
        log(f"run exited early (code {exit_code})")
    except BaseException:
        # Report while the log is still open, and to the terminal as well. The
        # previous arrangement closed sys.stderr in `finally`, so by the time the
        # interpreter tried to print the traceback it had nowhere to put it and
        # emitted "lost sys.stderr" plus a raw object dump instead.
        trace = traceback.format_exc()
        log("run FAILED:\n" + trace)
        exit_code = 1
        try:
            console_stderr.write("\n" + trace)
            console_stderr.write(f"\nThis traceback was also written to {ERROR_LOG_FILE}.\n")
            console_stderr.flush()
        except Exception:
            pass
    finally:
        close_output_files()
        sys.stderr = console_stderr
        try:
            LOG_STREAM.close()
        except Exception:
            pass

    sys.exit(exit_code)
