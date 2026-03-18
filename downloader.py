"""
Instagram Video Downloader
Downloads stories, reels, and post videos from Instagram.
Supports login for private accounts.
"""

import argparse
import os
import re
import sys
import getpass
from pathlib import Path

import instaloader


def patched_two_factor_login(context, two_factor_code):
    """Patched 2FA login using the correct Instagram API endpoint."""
    if not context.two_factor_auth_pending:
        raise instaloader.exceptions.InvalidArgumentException("No two-factor authentication pending.")
    (session, user, two_factor_id) = context.two_factor_auth_pending
    login = session.post(
        'https://www.instagram.com/api/v1/web/accounts/login/ajax/two_factor/',
        data={
            'username': user,
            'verificationCode': two_factor_code,
            'identifier': two_factor_id,
            'trust_signal': 'true',
        },
        allow_redirects=True,
    )
    resp_json = login.json()
    if resp_json.get('status') != 'ok':
        if 'message' in resp_json:
            raise instaloader.exceptions.BadCredentialsException("2FA error: {}".format(resp_json['message']))
        else:
            raise instaloader.exceptions.BadCredentialsException('2FA error: "{}" status.'.format(resp_json['status']))
    session.headers.update({'X-CSRFToken': login.cookies['csrftoken']})
    context._session = session
    context.username = user
    context.two_factor_auth_pending = None


def create_loader(username=None, session_file=None):
    """Create and configure an Instaloader instance, optionally logged in."""
    loader = instaloader.Instaloader(
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        filename_pattern="{date_utc:%Y%m%d_%H%M%S}_{shortcode}",
    )

    if session_file and os.path.exists(session_file):
        try:
            loader.load_session_from_file(username, session_file)
            print(f"[+] Loaded saved session for '{username}'.")
            return loader
        except Exception as e:
            print(f"[!] Could not load session file: {e}")

    if username:
        password = getpass.getpass(f"Password for '{username}': ")
        try:
            loader.login(username, password)
            print(f"[+] Logged in as '{username}'.")
            # Save session for future use
            if session_file:
                loader.save_session_to_file(session_file)
                print(f"[+] Session saved to '{session_file}'.")
        except instaloader.exceptions.BadCredentialsException:
            print("[!] Bad credentials. Check username/password.")
            sys.exit(1)
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            print("[!] Two-factor authentication required.")
            code = input("Enter 2FA code: ").strip()
            try:
                patched_two_factor_login(loader.context, code)
                print("[+] 2FA login successful.")
                if session_file:
                    loader.save_session_to_file(session_file)
            except Exception as e:
                print(f"[!] 2FA login failed: {e}")
                sys.exit(1)
        except Exception as e:
            print(f"[!] Login failed: {e}")
            sys.exit(1)

    return loader


def parse_url(url):
    """
    Parse an Instagram URL and return (content_type, identifier).
    Supported:
      - Post/Reel:  https://www.instagram.com/p/SHORTCODE/
                     https://www.instagram.com/reel/SHORTCODE/
      - Story:      https://www.instagram.com/stories/USERNAME/STORY_ID/
      - Profile:    https://www.instagram.com/USERNAME/
    """
    url = url.strip().rstrip("/")

    # Post or Reel
    m = re.match(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if m:
        return ("post", m.group(1))

    # Specific story
    m = re.match(r"https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9._]+)/(\d+)", url)
    if m:
        return ("story", (m.group(1), m.group(2)))

    # All stories from user: /stories/USERNAME/
    m = re.match(r"https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9._]+)/?$", url)
    if m:
        return ("profile_stories", m.group(1))

    # Profile (all stories)
    m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?$", url)
    if m:
        return ("profile_stories", m.group(1))

    return (None, None)


def download_post(loader, shortcode, output_dir):
    """Download a post or reel by shortcode."""
    print(f"[*] Fetching post/reel: {shortcode}")
    try:
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        if not post.is_video and not post.typename == "GraphSidecar":
            print("[!] This post does not contain a video.")
            return

        loader.dirname_pattern = str(output_dir)
        loader.download_post(post, target=Path(output_dir))
        print(f"[+] Downloaded to '{output_dir}/'")
    except instaloader.exceptions.LoginRequiredException:
        print("[!] Login required to access this content. Use --username to log in.")
    except Exception as e:
        print(f"[!] Error downloading post: {e}")


def download_story(loader, username, story_id, output_dir):
    """Download a specific story item by username and story media ID."""
    print(f"[*] Fetching story {story_id} from @{username}")
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        stories = loader.get_stories(userids=[profile.userid])

        found = False
        for story in stories:
            for item in story.get_items():
                if str(item.mediaid) == story_id:
                    loader.dirname_pattern = str(output_dir)
                    loader.download_storyitem(item, target=Path(output_dir))
                    print(f"[+] Downloaded story to '{output_dir}/'")
                    found = True
                    break
            if found:
                break

        if not found:
            print("[!] Story not found. It may have expired or you may need to log in.")
    except instaloader.exceptions.LoginRequiredException:
        print("[!] Login required to access stories. Use --username to log in.")
    except Exception as e:
        print(f"[!] Error downloading story: {e}")


def download_all_stories(loader, username, output_dir):
    """Download all current stories from a user."""
    print(f"[*] Fetching all stories from @{username}")
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        stories = loader.get_stories(userids=[profile.userid])

        count = 0
        for story in stories:
            for item in story.get_items():
                loader.dirname_pattern = str(output_dir)
                loader.download_storyitem(item, target=Path(output_dir))
                count += 1

        if count == 0:
            print("[!] No stories found. They may have expired or you may need to log in.")
        else:
            print(f"[+] Downloaded {count} story item(s) to '{output_dir}/'")
    except instaloader.exceptions.LoginRequiredException:
        print("[!] Login required to access stories. Use --username to log in.")
    except Exception as e:
        print(f"[!] Error downloading stories: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Instagram Video Downloader — download stories, reels, and post videos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download a reel (public)
  python3 downloader.py https://www.instagram.com/reel/ABC123/

  # Download a reel from a private account
  python3 downloader.py https://www.instagram.com/reel/ABC123/ --username myuser

  # Download a specific story
  python3 downloader.py https://www.instagram.com/stories/someuser/1234567890/ --username myuser

  # Download all stories from a user
  python3 downloader.py https://www.instagram.com/someuser/ --username myuser

  # Reuse a saved session (no password prompt after first login)
  python3 downloader.py https://www.instagram.com/reel/ABC123/ --username myuser --session session.instaloader
        """,
    )
    parser.add_argument("url", help="Instagram URL (post, reel, or story link)")
    parser.add_argument("--username", "-u", help="Instagram username for login (needed for private content/stories)")
    parser.add_argument("--session", "-s", help="Path to session file (saves/loads login session to avoid repeated logins)")
    parser.add_argument("--output", "-o", default="downloads", help="Output directory (default: downloads)")

    args = parser.parse_args()

    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)

    content_type, identifier = parse_url(args.url)

    if content_type is None:
        print(f"[!] Could not parse URL: {args.url}")
        print("    Supported formats:")
        print("      https://www.instagram.com/p/SHORTCODE/")
        print("      https://www.instagram.com/reel/SHORTCODE/")
        print("      https://www.instagram.com/stories/USERNAME/STORY_ID/")
        print("      https://www.instagram.com/USERNAME/")
        sys.exit(1)

    # Stories always require login
    if content_type in ("story", "profile_stories") and not args.username:
        print("[!] Downloading stories requires login. Please provide --username.")
        sys.exit(1)

    loader = create_loader(username=args.username, session_file=args.session)

    if content_type == "post":
        download_post(loader, identifier, output_dir)
    elif content_type == "story":
        username, story_id = identifier
        download_story(loader, username, story_id, output_dir)
    elif content_type == "profile_stories":
        download_all_stories(loader, identifier, output_dir)


if __name__ == "__main__":
    main()
