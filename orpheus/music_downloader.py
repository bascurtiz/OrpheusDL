import logging, os
import shutil
import unicodedata
from dataclasses import asdict
from time import strftime, gmtime
import json
from enum import Enum
import uuid
import time
import re
import platform
from concurrent.futures import ThreadPoolExecutor, as_completed

# Lazy import ffmpeg to avoid circular import issues in PyInstaller bundles
ffmpeg = None
Error = None

def _ensure_ffmpeg_imported():
    """Lazily import ffmpeg module to avoid circular import issues."""
    global ffmpeg, Error
    if ffmpeg is None:
        import ffmpeg as _ffmpeg
        ffmpeg = _ffmpeg
        Error = _ffmpeg.Error

from orpheus.tagging import tag_file
from utils.models import *
from utils.utils import *
from utils.exceptions import *

# --- Modular Spotify Import ---
try:
    from modules.spotify.spotify_api import SpotifyRateLimitDetectedError
except ModuleNotFoundError:
    # Define a dummy exception if Spotify module isn't found
    # This allows 'except SpotifyRateLimitDetectedError:' blocks elsewhere
    # in this file to still compile, though they will never be triggered.
    class SpotifyRateLimitDetectedError(Exception):
        pass

# Platform colors from GUI (hex colors converted to closest ANSI equivalents)
PLATFORM_COLORS = {
    "tidal": "\033[96m",         # Bright cyan (#33ffe7 -> bright cyan)
    "jiosaavn": "\x1b[96m",      # Cyan (#1eccb0 -> cyan) 
    "apple music": "\033[91m",   # Bright red (#FA586A -> bright red)
    "beatport": "\033[92m",      # Bright green (#00ff89 -> bright green)
    "beatsource": "\033[94m",    # Bright blue (#16a8f4 -> bright blue)
    "deezer": "\033[38;5;129m",        # Bright magenta (#a238ff -> bright magenta)
    "qobuz": "\033[34m",         # Blue (#0070ef -> blue)
    "soundcloud": "\033[38;5;208m",    # Bright yellow/orange (#ff5502 -> bright yellow as closest)
    "spotify": "\033[32m",       # Green (#1cc659 -> green)
    "napster": "\033[94m",       # Bright blue (#295EFF -> bright blue)
    "kkbox": "\033[36m",         # Cyan (#27B1D8 -> cyan)
    "idagio": "\033[35m",        # Magenta (#5C34FE -> magenta)
    "bugs": "\033[31m",          # Red (#FF3B28 -> red)
    "nugs": "\033[31m"           # Red (#C83B30 -> red)
}

RESET_COLOR = "\033[0m"

def get_colored_platform_name(service_name):
    """Get the platform name with appropriate ANSI color coding"""
    if not service_name:
        return "Unknown"
    
    # Normalize the service name to lowercase for matching
    normalized_name = service_name.lower()
    
    # Get the color for this platform
    color_code = PLATFORM_COLORS.get(normalized_name, "")
    
    # Return colored platform name
    if color_code:
        return f"{color_code}{service_name}{RESET_COLOR}"
    else:
        return service_name

def beauty_format_seconds(seconds: int) -> str:
    time_data = gmtime(seconds)

    time_format = "%Mm:%Ss"
    # if seconds are higher than 3600s also add the hour format
    if time_data.tm_hour > 0:
        time_format = "%Hh:" + time_format
    # TODO: also add days to time_format if hours > 24?

    # return the formatted time string
    return strftime(time_format, time_data)


def simplify_error_message(error_str: str) -> str:
    """Convert complex error messages into user-friendly one-liners"""
    error_lower = error_str.lower()
    
    # Track unavailable/not found errors
    if any(phrase in error_lower for phrase in ['track is unavailable', 'track unavailable', 'unavailable']):
        return "This song is unavailable."
    
    # JSON API error responses with 404 code (e.g., Qobuz)
    if '"code":404' in error_str or '"code": 404' in error_str:
        return "This song is unavailable."
    
    # HTTP status code 404 in plain text
    if 'status code 404' in error_lower or 'error 404' in error_lower:
        return "This song is unavailable."
    
    # Apple Music errors
    if 'apple music' in error_lower:
        if 'unexpected error during download' in error_lower:
            # Check if it's likely an FFmpeg/processing issue
            if any(keyword in error_lower for keyword in ['ffmpeg', 'remux', 'processing', 'legacy remux', 'expected']):
                return "Apple Music streaming error (FFmpeg required for processing)"
            return "Apple Music download error"
        elif any(keyword in error_lower for keyword in ['ffmpeg', 'remux', 'processing', 'legacy remux']):
            return "Apple Music streaming error (FFmpeg required for processing)"
        elif 'not authenticated' in error_lower or 'cookies.txt' in error_lower:
            return "Apple Music authentication error (cookies.txt required)"
        return "Apple Music error"
    
    # SoundCloud HLS streaming errors
    if 'soundcloud' in error_lower and ('hls' in error_lower or 'hls_unexpected_error_in_try_block' in error_lower):
        if 'ffmpeg' in error_lower or 'url' in error_lower or 'hls_unexpected_error_in_try_block' in error_lower:
            return "SoundCloud streaming error (FFmpeg required for HLS streams)"
        return "SoundCloud streaming error"
    
    # Generic FFmpeg errors
    if 'ffmpeg' in error_lower and ('process failed' in error_lower or 'error opening' in error_lower):
        return "Audio processing error (FFmpeg)"
    
    # Network/URL errors
    if any(phrase in error_lower for phrase in ['url', 'network', 'connection', 'timeout']):
        return "Network/connection error"
    
    # File system errors
    if any(phrase in error_lower for phrase in ['no such file', 'permission denied', 'file not found']):
        return "File system error"
    
    # Authentication errors
    if any(phrase in error_lower for phrase in ['auth', 'login', 'credential', 'token']):
        return "Authentication error"
    
    # Rate limiting
    if any(phrase in error_lower for phrase in ['rate limit', 'too many requests', '429']):
        return "Rate limited - too many requests"
    
    # Generic fallback - try to extract the most relevant part
    if ':' in error_str:
        # Take the last part after the final colon, which is usually the most specific error
        parts = error_str.split(':')
        last_part = parts[-1].strip()
        if len(last_part) > 10 and len(last_part) < 100:  # Reasonable length
            return last_part
    
    # If error is too long, truncate it
    if len(error_str) > 80:
        return error_str[:77] + "..."
    
    return error_str

# Helper function to serialize Enums for JSON
def json_enum_serializer(obj):
    if isinstance(obj, Enum):
        return obj.name
    # Let the default encoder raise TypeError for other unserializable types
    raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')


class Downloader:
    def __init__(self, settings, module_controls, oprinter, path, use_ansi_colors=True):
        self.global_settings = settings
        self.module_controls = module_controls
        self.oprinter = oprinter
        self.path = path
        self.service = None
        self.service_name = None
        self.download_mode = None
        self.third_party_modules = None
        self.temp_dir = None  # Will be set by core.py
        self.indent_number = 0
        self.module_list = module_controls['module_list']
        self.module_settings = module_controls['module_settings']
        self.loaded_modules = module_controls['loaded_modules']
        self.load_module = module_controls['module_loader']
        self.full_settings = None  # Will be set by core.py
        self.use_ansi_colors = use_ansi_colors

        self.print = self.oprinter.oprint
        self.set_indent_number = self.oprinter.set_indent_number

    def _get_spotify_pause_seconds(self):
        """Get the Spotify pause duration from settings, with fallback to default"""
        try:
            if hasattr(self, 'full_settings') and self.full_settings and 'modules' in self.full_settings and 'spotify' in self.full_settings['modules']:
                return int(self.full_settings['modules']['spotify'].get('download_pause_seconds', 30))
        except (KeyError, ValueError, TypeError):
            pass
        return 30  # Default fallback

    def _get_status_symbols(self):
        """Get platform-appropriate status symbols with universal colors"""
        # ANSI color codes that work across Windows, macOS, and Linux
        GREEN = '\033[92m'    # Green for success
        YELLOW = '\033[33m'   # Golden yellow for skip/warning (closer to #CCA700)
        RED = '\033[91m'      # Red for error
        GRAY = '\033[90m'     # Gray for status text
        RESET = '\033[0m'     # Reset to default color
        
        if not self.use_ansi_colors:
            return {
                'success': '✓',
                'skip': '▶',
                'error': '✗',
                'warning': '⚠',
                'gray_text': '',
                'yellow_text': '',
                'red_text': '',
                'reset': ''
            }
        
        # Use ASCII symbols for Windows Command Prompt compatibility
        if platform.system() == 'Windows':
            return {
                'success': f'{GREEN}+{RESET}',      # Green plus sign for success
                'skip': f'{YELLOW}>{RESET}',        # Yellow greater than for skip/already exists
                'error': f'{RED}x{RESET}',          # Red lowercase x for error/failed
                'warning': f'{YELLOW}!{RESET}',     # Yellow exclamation for warning/rate limited
                'gray_text': GRAY,                  # Gray for general status text
                'yellow_text': YELLOW,              # Yellow for "(already exists)" text
                'red_text': RED,                    # Red for "(failed)" text
                'reset': RESET
            }
        else:
            # Use Unicode symbols for Unix/macOS terminals (better Unicode support)
            return {
                'success': f'{GREEN}✓{RESET}',      # Green check mark
                'skip': f'{YELLOW}▶{RESET}',        # Yellow play button
                'error': f'{RED}✗{RESET}',          # Red ballot X for error/failed
                'warning': f'{YELLOW}⚠{RESET}',     # Yellow warning sign
                'gray_text': GRAY,                  # Gray for general status text
                'yellow_text': YELLOW,              # Yellow for "(already exists)" text
                'red_text': RED,                    # Red for "(failed)" text
                'reset': RESET
            }

    def create_temp_filename(self):
        """Create a temporary filename in the temp directory"""
        if not self.temp_dir:
            # If temp_dir is not set, create it in the current directory
            self.temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(self.temp_dir, exist_ok=True)
        return os.path.join(self.temp_dir, str(uuid.uuid4()))

    def search_by_tags(self, module_name, track_info: TrackInfo):
        return self.loaded_modules[module_name].search(DownloadTypeEnum.track, f'{track_info.name} {" ".join(track_info.artists)}', track_info=track_info)

    def _concurrent_download_tracks(self, track_list, download_args_list, concurrent_downloads, performance_summary_indent=0):
        """Helper method to download tracks concurrently using asyncio + aiohttp"""
        if concurrent_downloads <= 1:
            # Fallback to sequential download if concurrent_downloads is 1 or less
            self.print("Using sequential downloads (sync)")
            results = []
            for i, (track_info, args) in enumerate(zip(track_list, download_args_list)):
                try:
                    result = self.download_track(**args)
                    results.append((i, result, None))
                except Exception as e:
                    results.append((i, None, e))
            return results
        
        # Use asyncio + aiohttp for concurrent downloads
        import asyncio
        import time
        from utils.utils import create_aiohttp_session, download_file_async
        
        # Store original print method
        original_print = self.print
        total_tracks = len(track_list)
        results = [None] * total_tracks
        
        # Performance tracking
        start_time = time.time()
        total_bytes_downloaded = 0
        download_times = []
        concurrent_active = 0
        max_concurrent_seen = 0
        
        async def download_worker_async(session, index, args):
            """Async worker function to download a single track - OPTIMIZED VERSION"""
            nonlocal concurrent_active, max_concurrent_seen, total_bytes_downloaded
            
            # Track concurrency
            concurrent_active += 1
            max_concurrent_seen = max(max_concurrent_seen, concurrent_active)
            
            track_start_time = time.time()
            bytes_downloaded = 0
            
            try:
                # Get track info ONCE and pass it to the download function
                track_id = args['track_id']
                track_name = f"Track {track_id}"
                
                # Get track info and download info (API calls) - DO THIS ONCE PER TRACK IN THREAD POOL
                try:
                    quality_tier = QualityEnum[self.global_settings['general']['download_quality'].upper()]
                    codec_options = CodecOptions(
                        spatial_codecs = self.global_settings['codecs']['spatial_codecs'],
                        proprietary_codecs = self.global_settings['codecs']['proprietary_codecs'],
                    )
                    
                    # CRITICAL FIX: Move API calls to thread pool to avoid blocking event loop
                    loop = asyncio.get_event_loop()
                    
                    # SINGLE API CALL: Get track info once - IN THREAD POOL
                    # Create a wrapper function to handle the extra_kwargs properly
                    def get_track_info_wrapper():
                        return self.service.get_track_info(track_id, quality_tier, codec_options, **args.get('extra_kwargs', {}))
                    
                    track_info = await loop.run_in_executor(None, get_track_info_wrapper)
                    track_name = f"{', '.join(track_info.artists)} - {track_info.name}"
                    
                    # Check if file already exists BEFORE getting download info (for temp file modules like Deezer)
                    if track_info:
                        track_location = self._create_track_location(args.get('album_location', ''), track_info)
                        if await loop.run_in_executor(None, os.path.isfile, track_location):
                            return (index, track_name, "SKIPPED", None, None, 0, 0)
                    
                    # SINGLE API CALL: Get download info once - IN THREAD POOL
                    def get_download_info_wrapper():
                        # Check if track_info has download_extra_kwargs (like Qobuz, TIDAL, Deezer)
                        if hasattr(track_info, 'download_extra_kwargs') and track_info.download_extra_kwargs:
                            return self.service.get_track_download(**track_info.download_extra_kwargs)
                        else:
                            # Try the full signature first (for modules that support it)
                            try:
                                return self.service.get_track_download(track_id, quality_tier, codec_options, **args.get('extra_kwargs', {}))
                            except TypeError:
                                # Fallback for modules with simpler signatures
                                return self.service.get_track_download(track_id, quality_tier)
                                
                    download_info = await loop.run_in_executor(None, get_download_info_wrapper)
                    
                except Exception as e:
                    error_msg = str(e)
                    track_name = track_id
                    return (index, track_name, f"Could not get track/download info: {error_msg}", None, Exception(f"Could not get track/download info for {track_id}: {error_msg}"), 0, 0)

                # Pass both track_info and download_info to avoid double API calls
                result = await self._download_track_async(
                    session, 
                    track_info=track_info, 
                    download_info=download_info,
                    **args, 
                    verbose=False
                )

                track_duration = time.time() - track_start_time

                # Handle the return format from _download_track_async
                if isinstance(result, tuple):
                    # New format: (file_location, bytes_downloaded)
                    file_location, bytes_downloaded = result
                    if file_location is None:
                        # Track already existed or failed
                        if bytes_downloaded == 0:
                            return (index, track_name, "ERROR", None, Exception("Download failed"), 0, track_duration)
                        else:
                            return (index, track_name, "ERROR", None, Exception("Download failed"), 0, track_duration)
                    else:
                        # Successfully downloaded
                        return (index, track_name, None, file_location, None, bytes_downloaded, track_duration)
                else:
                    # Old format compatibility - estimate download size
                    if result is None:
                        # Track download failed - report as error
                        return (index, track_name, "ERROR", None, Exception("Download failed"), 0, track_duration)
                    elif result == "ALREADY_EXISTS":
                        # Track already existed - report as skipped
                        return (index, track_name, "SKIPPED", None, None, 0, track_duration)
                    elif result == "RATE_LIMITED":
                        # Rate limited - report as rate limited
                        return (index, track_name, "RATE_LIMITED", "RATE_LIMITED", None, 0, track_duration)
                    elif isinstance(result, str) and result not in ["ALREADY_EXISTS", "RATE_LIMITED"]:
                        # Specific error messages - pass them through as status
                        return (index, track_name, result, None, Exception(result), 0, track_duration)
                    else:
                        # Successfully downloaded - estimate 8MB
                        bytes_downloaded = 8 * 1024 * 1024  # 8MB estimate
                        return (index, track_name, None, result, None, bytes_downloaded, track_duration)

            except Exception as e:
                track_duration = time.time() - track_start_time
                return (index, f"Track {args.get('track_id', 'Unknown')}", e, None, e, 0, track_duration)
            finally:
                concurrent_active -= 1
        
        async def run_concurrent_downloads():
            """Main async function to coordinate downloads"""
            nonlocal total_bytes_downloaded
            
            # Disable progress bars globally if the setting is disabled
            from utils.utils import set_progress_bars_enabled
            progress_bar_setting = self.global_settings['general'].get('progress_bar', False)
            set_progress_bars_enabled(progress_bar_setting)
            
            async with create_aiohttp_session() as session:
                # Create semaphore to limit concurrent downloads
                semaphore = asyncio.Semaphore(concurrent_downloads)
                
                async def bounded_download(index, args):
                    async with semaphore:
                        return await download_worker_async(session, index, args)
                
                # Create tasks for all downloads
                tasks = [bounded_download(i, args) for i, args in enumerate(download_args_list)]
                
                # Progress tracking
                symbols = self._get_status_symbols()
                completed_count = 0
                total_digits = len(str(total_tracks))
                
                # Process downloads as they complete (OUT OF ORDER!)
                results_temp = []
                
                for coro in asyncio.as_completed(tasks):
                    try:
                        result = await coro
                        index, track_name, status, download_result, error, bytes_dl, duration = result
                        
                        completed_count += 1
                        total_bytes_downloaded += bytes_dl
                        if duration > 0:
                            download_times.append(duration)
                        
                        # Display progress with sequential numbering for user-friendly tracking
                        track_number = completed_count  # Use sequential numbering (1-based)
                        
                        if status == "SKIPPED":
                            self.print(f"{track_number:0{total_digits}d}/{total_tracks} {symbols['skip']} {track_name} {symbols['yellow_text']}(already exists){symbols['reset']}", drop_level=performance_summary_indent)
                        elif status == "RATE_LIMITED":
                            self.print(f"{track_number:0{total_digits}d}/{total_tracks} {symbols['warning']} {track_name} (rate limited)", drop_level=performance_summary_indent)
                        elif status is not None:
                            # Error case
                            if isinstance(status, str) and status.startswith("Could not get track info: "):
                                error_msg = status.replace("Could not get track info: ", "")
                                simplified_error = simplify_error_message(error_msg)
                                self.print(f"{track_number:0{total_digits}d}/{total_tracks} {symbols['error']} Track {track_name}: {simplified_error} {symbols['red_text']}(failed){symbols['reset']}", drop_level=performance_summary_indent)
                            else:
                                simplified_error = simplify_error_message(str(status))
                                self.print(f"{track_number:0{total_digits}d}/{total_tracks} {symbols['error']} {track_name}: {simplified_error} {symbols['red_text']}(failed){symbols['reset']}", drop_level=performance_summary_indent)
                        else:
                            # Success
                            self.print(f"{track_number:0{total_digits}d}/{total_tracks} {symbols['success']} {track_name}", drop_level=performance_summary_indent)
                        
                        # Flush output to ensure immediate display in GUI
                        import sys
                        if hasattr(sys.stdout, 'flush'):
                            sys.stdout.flush()
                        
                        # Store result for final processing
                        results_temp.append((index, download_result, error))
                        
                    except Exception as e:
                        completed_count += 1
                        self.print(f"???/{total_tracks} {symbols['error']} Track (unknown): {simplify_error_message(str(e))} {symbols['red_text']}(failed){symbols['reset']}", drop_level=performance_summary_indent)
                        # Flush output to ensure immediate display in GUI
                        import sys
                        if hasattr(sys.stdout, 'flush'):
                            sys.stdout.flush()
                        results_temp.append((len(results_temp), None, e))
                
                return results_temp
        
        # Run the async downloads with Windows compatibility
        try:
            import platform
            
            self.print(f"Using {concurrent_downloads} concurrent downloads for {total_tracks} tracks", drop_level=performance_summary_indent)
            
            if platform.system() == 'Windows':
                # For Windows, set the event loop policy to avoid SelectorEventLoop issues
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            if hasattr(asyncio, 'run'):
                # Python 3.7+
                results_temp = asyncio.run(run_concurrent_downloads())
            else:
                # Python 3.6 compatibility
                if platform.system() == 'Windows':
                    loop = asyncio.ProactorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    results_temp = loop.run_until_complete(run_concurrent_downloads())
                finally:
                    loop.close()
                    
        except Exception as e:
            original_print(f"❌ Error in async downloads: {e}", drop_level=1)
            original_print("🔄 Falling back to sync downloads")
            # Fallback to sequential downloads
            results = []
            for i, (track_info, args) in enumerate(zip(track_list, download_args_list)):
                try:
                    result = self.download_track(**args)
                    results.append((i, result, None))
                except Exception as e:
                    results.append((i, None, e))
            return results
        
        # Performance summary
        total_time = time.time() - start_time
        if total_time > 0:
            avg_concurrent = len(download_times) / total_time if download_times else 0
            total_mb = total_bytes_downloaded / (1024 * 1024)
            overall_speed_mbps = (total_mb / total_time) * 8 if total_time > 0 else 0
            avg_track_time = sum(download_times) / len(download_times) if download_times else 0
            
            # Format time as minutes:seconds
            minutes = int(total_time // 60)
            seconds = total_time % 60
            if minutes > 0:
                time_str = f"{minutes}m {seconds:.1f}s"
            else:
                time_str = f"{seconds:.1f}s"
            
            # Show performance summary in order: Download speed, Download time
            # Only show speed metrics if data was actually downloaded
            if total_mb > 0:
                original_print(f"Download speed: {overall_speed_mbps:.0f} Mbps", drop_level=performance_summary_indent)
                original_print(f"Download time: {time_str}", drop_level=performance_summary_indent)
            else:
                # Don't assume tracks already existed - they might have failed
                original_print(f"Download time: {time_str}", drop_level=performance_summary_indent)
        
        # Convert results to expected format
        for index, download_result, error in results_temp:
            if index < len(results):
                results[index] = (index, download_result, error)
        
        # Count actual results for final summary
        actual_downloaded = sum(1 for r in results if r and r[2] is None and r[1] is not None)  # Newly downloaded
        actual_already_existed = sum(1 for r in results if r and r[2] is None and r[1] is None)  # Already existed
        actual_failed = sum(1 for r in results if r and r[2] is not None)  # Failed with error
        
        # Show final summary only when there are failures
        if actual_failed > 0:
            # Check if most failures are SoundCloud FFmpeg-related
            ffmpeg_errors = sum(1 for r in results if r and r[2] is not None and 
                              isinstance(r[2], Exception) and 
                              'FFmpeg required for HLS streams' in str(r[2]))
            
            if actual_downloaded > 0 and actual_already_existed > 0:
                original_print(f"Summary: {actual_downloaded} downloaded, {actual_already_existed} already existed, {actual_failed} failed.", drop_level=performance_summary_indent)
            elif actual_downloaded > 0:
                original_print(f"Summary: {actual_downloaded} downloaded, {actual_failed} failed.", drop_level=performance_summary_indent)
            elif actual_already_existed > 0:
                original_print(f"Summary: {actual_already_existed} already existed, {actual_failed} failed.", drop_level=performance_summary_indent)
            else:
                original_print(f"Summary: {actual_failed} failed.", drop_level=performance_summary_indent)
            
            # Add helpful FFmpeg message if many SoundCloud HLS errors occurred
            if ffmpeg_errors > 0 and ffmpeg_errors >= actual_failed * 0.8:  # 80% or more are FFmpeg errors
                original_print("", drop_level=performance_summary_indent)  # Blank line
                original_print("NOTE: Most failures are due to missing FFmpeg.", drop_level=performance_summary_indent)
                original_print("SoundCloud requires FFmpeg for HLS stream processing.", drop_level=performance_summary_indent)
                original_print("Please install FFmpeg or configure it in Settings > Global > Advanced.", drop_level=performance_summary_indent)
        
        return results


    def _add_track_m3u_playlist(self, m3u_playlist: str, track_info: TrackInfo, track_location: str):
        if self.global_settings['playlist']['extended_m3u']:
            with open(m3u_playlist, 'a', encoding='utf-8') as f:
                # if no duration exists default to -1
                duration = track_info.duration if track_info.duration else -1
                # write the extended track header
                f.write(f'#EXTINF:{duration}, {track_info.artists[0]} - {track_info.name}\n')

        with open(m3u_playlist, 'a', encoding='utf-8') as f:
            if self.global_settings['playlist']['paths_m3u'] == "absolute":
                # add the absolute paths to the playlist
                f.write(f'{os.path.abspath(track_location)}\n')
            else:
                # add the relative paths to the playlist by subtracting the track_location with the m3u_path
                f.write(f'{os.path.relpath(track_location, os.path.dirname(m3u_playlist))}\n')

            # add an extra new line to the extended format
            f.write('\n') if self.global_settings['playlist']['extended_m3u'] else None

    def download_playlist(self, playlist_id, custom_module=None, extra_kwargs=None):
        import time
        playlist_start_time = time.time()  # Track total playlist download time
        
        self.set_indent_number(1)

        service_name_lower = ""
        if hasattr(self, 'service_name') and self.service_name:
            service_name_lower = self.service_name.lower()

        # Prepare kwargs for get_playlist_info, making a copy to modify
        kwargs_for_playlist_info = {}
        if extra_kwargs:
            kwargs_for_playlist_info.update(extra_kwargs)

        if service_name_lower in ['beatport', 'beatsource']:
            if 'data' in kwargs_for_playlist_info:
                logging.debug(f"Removing 'data' from extra_kwargs for {self.service_name}.get_playlist_info as it is unexpected.")
                kwargs_for_playlist_info.pop('data', None)

        playlist_info: PlaylistInfo = self.service.get_playlist_info(playlist_id, **kwargs_for_playlist_info)
        if not playlist_info:
            logging.warning(f"Could not retrieve playlist info for {playlist_id} from {self.service_name}. Skipping playlist.")
            return []
        
        self.print(f'=== Downloading playlist {playlist_info.name} ({playlist_id}) ===', drop_level=1)
        self.print(f'Playlist creator: {playlist_info.creator}' + (f' ({playlist_info.creator_id})' if playlist_info.creator_id else ''))
        if playlist_info.release_year: self.print(f'Playlist creation year: {playlist_info.release_year}')
        if playlist_info.duration: self.print(f'Duration: {beauty_format_seconds(playlist_info.duration)}')
        number_of_tracks = len(playlist_info.tracks)
        self.print(f'Number of tracks: {number_of_tracks!s}')
        
        # Sanitize and shorten playlist name for filesystem
        safe_playlist_name = sanitise_name(playlist_info.name)
        if len(safe_playlist_name) > 50: # Truncate long names
            safe_playlist_name = safe_playlist_name[:50]

        playlist_tags = {k: sanitise_name(v) for k, v in asdict(playlist_info).items()}
        playlist_tags['name'] = safe_playlist_name # Use the safe name for path formatting
        playlist_tags['explicit'] = ' [E]' if playlist_info.explicit else ''
        playlist_path_formatted_name = self.global_settings['formatting']['playlist_format'].format(**playlist_tags)
        playlist_path = os.path.join(self.path, playlist_path_formatted_name)
        # fix path byte limit
        playlist_path = fix_byte_limit(playlist_path) + '/'
        os.makedirs(playlist_path, exist_ok=True)
        
        if playlist_info.cover_url:
            self.print('Downloading playlist cover')
            download_file(playlist_info.cover_url, f'{playlist_path}cover.{playlist_info.cover_type.name}', artwork_settings=self._get_artwork_settings())
        
        colored_platform = get_colored_platform_name(self.module_settings[self.service_name].service_name)
        self.print(f'Platform: {colored_platform}')
        
        if playlist_info.animated_cover_url and self.global_settings['covers']['save_animated_cover']:
            self.print('Downloading animated playlist cover')
            download_file(playlist_info.animated_cover_url, playlist_path + 'cover.mp4', enable_progress_bar=self.global_settings['general'].get('progress_bar', False))
        
        if playlist_info.description:
            with open(playlist_path + 'description.txt', 'w', encoding='utf-8') as f: f.write(playlist_info.description)

        m3u_playlist_path = None
        if self.global_settings['playlist']['save_m3u']:
            if self.global_settings['playlist']['paths_m3u'] not in {"absolute", "relative"}:
                raise ValueError(f'Invalid value for paths_m3u: "{self.global_settings["playlist"]["paths_m3u"]}",'
                                 f' must be either "absolute" or "relative"')

            m3u_playlist_path = os.path.join(playlist_path, f'{safe_playlist_name}.m3u')

            # create empty file
            with open(m3u_playlist_path, 'w', encoding='utf-8') as f:
                f.write('')

            # if extended format add the header
            if self.global_settings['playlist']['extended_m3u']:
                with open(m3u_playlist_path, 'a', encoding='utf-8') as f:
                    f.write('#EXTM3U\n\n')

        tracks_errored = set()
        rate_limited_tracks = [] # Initialize list for deferred tracks
        if custom_module:
            supported_modes = self.module_settings[custom_module].module_supported_modes 
            if ModuleModes.download not in supported_modes and ModuleModes.playlist not in supported_modes:
                raise Exception(f'Module "{custom_module}" cannot be used to download a playlist') # TODO: replace with ModuleDoesNotSupportAbility
            self.print(f'Service used for downloading: {self.module_settings[custom_module].service_name}')
            original_service = str(self.service_name)
            self.load_module(custom_module)
            for index, track_id in enumerate(playlist_info.tracks, start=1):
                self.set_indent_number(2)
                print()
                self.print(f'Track {index}/{number_of_tracks}', drop_level=1)
                quality_tier = QualityEnum[self.global_settings['general']['download_quality'].upper()]
                codec_options = CodecOptions(
                    spatial_codecs = self.global_settings['codecs']['spatial_codecs'],
                    proprietary_codecs = self.global_settings['codecs']['proprietary_codecs'],
                )
                track_info: TrackInfo = self.loaded_modules[original_service].get_track_info(track_id, quality_tier, codec_options, **playlist_info.track_extra_kwargs)
                
                self.service = self.loaded_modules[custom_module]
                self.service_name = custom_module
                results = self.search_by_tags(custom_module, track_info)
                track_id_new = results[0].result_id if len(results) else None
                
                if track_id_new:
                    self.download_track(track_id_new, album_location=playlist_path, track_index=index, number_of_tracks=number_of_tracks, indent_level=2, m3u_playlist=m3u_playlist_path, extra_kwargs=results[0].extra_kwargs)
                else:
                    tracks_errored.add(f'{track_info.name} - {track_info.artists[0]}')
                    if ModuleModes.download in self.module_settings[original_service].module_supported_modes:
                        self.service = self.loaded_modules[original_service]
                        self.service_name = original_service
                        self.print(f'Track {track_info.name} not found, using the original service as a fallback', drop_level=1)
                        self.download_track(track_id, album_location=playlist_path, track_index=index, number_of_tracks=number_of_tracks, indent_level=2, m3u_playlist=m3u_playlist_path, extra_kwargs=playlist_info.track_extra_kwargs)
                    else:
                        self.print(f'Track {track_info.name} not found, skipping')
        else:
            # Get concurrent downloads setting
            concurrent_downloads = self.global_settings['general'].get('concurrent_downloads', 1)
            
            # Force sequential downloads for specific modules or when concurrent_downloads is 1
            service_name_lower = ""
            if hasattr(self, 'service_name') and self.service_name:
                service_name_lower = self.service_name.lower()
            
            # Check if sequential downloads should be forced
            force_sequential = False
            sequential_reason = ""
            
            if concurrent_downloads == 1:
                force_sequential = True
                sequential_reason = "concurrent_downloads setting is 1"
            elif service_name_lower == 'spotify':
                force_sequential = True
                sequential_reason = "Spotify (rate limiting protection)"
            elif service_name_lower == 'applemusic':
                force_sequential = True
                sequential_reason = "Apple Music"
            
            if force_sequential:
                concurrent_downloads = 1
                print()  # Add blank line before sequential downloads message
                self.print(f"Using sequential downloads for {sequential_reason}")
            
            if concurrent_downloads > 1 and len(playlist_info.tracks) > 1:
                # Prepare download arguments for all tracks
                download_args_list = []
                for index, track_id_or_info in enumerate(playlist_info.tracks, start=1):
                    actual_track_id_str_for_download = track_id_or_info.id if isinstance(track_id_or_info, TrackInfo) else str(track_id_or_info)
                    
                    download_args = {
                        'track_id': actual_track_id_str_for_download,
                        'album_location': playlist_path,
                        'track_index': index,
                        'number_of_tracks': number_of_tracks,
                        'indent_level': 1,
                        'm3u_playlist': m3u_playlist_path,
                        'extra_kwargs': playlist_info.track_extra_kwargs
                    }
                    download_args_list.append(download_args)
                
                # Download tracks concurrently
                results = self._concurrent_download_tracks(playlist_info.tracks, download_args_list, concurrent_downloads, performance_summary_indent=0)
                
                # Process results - only collect rate-limited tracks for retry
                # (Errors are already reported by concurrent download progress monitor)
                for index, (original_index, result, error) in enumerate(results):
                    if error and result == "RATE_LIMITED":
                        actual_track_id_str_for_download = download_args_list[original_index]['track_id']
                        rate_limited_tracks.append({
                            'id': actual_track_id_str_for_download,
                            'extra_kwargs': playlist_info.track_extra_kwargs,
                            'original_index': original_index + 1
                        })
                    elif result == "RATE_LIMITED":
                        actual_track_id_str_for_download = download_args_list[original_index]['track_id']
                        rate_limited_tracks.append({
                            'id': actual_track_id_str_for_download,
                            'extra_kwargs': playlist_info.track_extra_kwargs,
                            'original_index': original_index + 1
                        })
            else:
                # Fallback to sequential downloads
                for index, track_id_or_info in enumerate(playlist_info.tracks, start=1):
                    self.set_indent_number(2)
                    print() # Add spacing between track attempts
                    # Only show "Pass 1" for Spotify (which has retry passes)
                    pass_indicator = " (Pass 1)" if service_name_lower == 'spotify' else ""
                    self.print(f'Track {index}/{number_of_tracks}{pass_indicator}', drop_level=1)
                    
                    # Determine the actual track ID string to use for download_track
                    actual_track_id_str_for_download = track_id_or_info.id if isinstance(track_id_or_info, TrackInfo) else str(track_id_or_info)
                    
                    download_result = self.download_track(
                        actual_track_id_str_for_download,
                        album_location=playlist_path,
                        track_index=index,
                        number_of_tracks=number_of_tracks,
                        indent_level=1,
                        m3u_playlist=m3u_playlist_path,
                        extra_kwargs=playlist_info.track_extra_kwargs
                    )
                    
                    # Add pause between downloads for Spotify to prevent rate limiting
                    # Only pause if track was actually downloaded (not skipped) and not the last track
                    if (service_name_lower == 'spotify' and index < number_of_tracks and 
                        download_result is not None and download_result != "RATE_LIMITED"):
                        pause_seconds = self._get_spotify_pause_seconds()
                        self.print(f'Pausing {pause_seconds} seconds to prevent rate limiting...', drop_level=1)
                        time.sleep(pause_seconds)
                    
                    if download_result == "RATE_LIMITED":
                        logging.info(f"Deferring track {actual_track_id_str_for_download} due to rate limit.")
                        rate_limited_tracks.append({
                            'id': actual_track_id_str_for_download, # Store the string ID
                            'extra_kwargs': playlist_info.track_extra_kwargs,
                            'original_index': index
                        })
                    elif m3u_playlist_path: # Add to M3U only if download didn't fail/get deferred
                        # Need to get track_info again or ensure download_track provides location
                        # This part needs refinement - how to get track_location if download succeeds?
                        # For now, assume download_track handles its own M3U addition upon success if needed.
                        pass

        # --- Second Pass for Rate-Limited Tracks --- 
        if rate_limited_tracks:
            self.set_indent_number(1)
            print() # Spacing
            if service_name_lower == 'applemusic':
                self.print(f"--- Retrying {len(rate_limited_tracks)} failed tracks ---", drop_level=1)
                self.print("Using sequential downloads for Apple Music retries", drop_level=1)
            else:
                self.print(f"--- Retrying {len(rate_limited_tracks)} rate-limited tracks ---", drop_level=1)
            for i, retry_item in enumerate(rate_limited_tracks):
                self.set_indent_number(2)
                print() # Spacing
                self.print(f'Track {retry_item["original_index"]}/{number_of_tracks} (Retry Pass)', drop_level=1)
                # retry_item['id'] is already a string ID
                self.download_track(
                    retry_item['id'],
                    album_location=playlist_path,
                    track_index=retry_item["original_index"],
                    number_of_tracks=number_of_tracks,
                    indent_level=1,
                    m3u_playlist=m3u_playlist_path, # Pass M3U path again
                    extra_kwargs=retry_item['extra_kwargs']
                )
                # Add pause between retry tracks (except for the last one)
                if i < len(rate_limited_tracks) - 1:
                    print()
                    if service_name_lower == 'applemusic':
                        self.print("Pausing 2 seconds before retry...", drop_level=1)
                        time.sleep(2)
                    else:
                        self.print("Pausing 30 seconds to prevent rate limiting...", drop_level=1)
                        time.sleep(30)
                # Note: M3U handling for retried tracks still needs consideration
        else:
            # Only show rate limiting message for Spotify (where it's relevant)
            if service_name_lower == 'spotify':
                print()  # Add blank line before message
                self.print("No tracks were deferred due to rate limiting.")
                print()  # Add blank line after message

        # --- Final Summary ---
        self.set_indent_number(1)
        
        symbols = self._get_status_symbols()
        self.print(f'=== {symbols["success"]} Playlist completed ===', drop_level=1)
        # Add 2 empty lines after playlist completion for visual separation
        print()
        print()
        if tracks_errored: logging.debug('Permanently failed tracks (non-rate-limit): ' + ', '.join(tracks_errored))

    @staticmethod
    def _get_artist_initials_from_name(album_info: AlbumInfo) -> str:
        # Remove "the" from the inital string
        initial = album_info.artist.lower()
        if album_info.artist.lower().startswith('the'):
            initial = initial.replace('the ', '')[0].upper()

        # Unicode fix
        initial = unicodedata.normalize('NFKD', initial[0]).encode('ascii', 'ignore').decode('utf-8')

        # Make the initial upper if it's alpha
        initial = initial.upper() if initial.isalpha() else '#'

        return initial

    def _create_album_location(self, path: str, album_id: str, album_info: AlbumInfo) -> str:
        # Clean up album tags and add special explicit and additional formats
        album_tags = {k: sanitise_name(v) for k, v in asdict(album_info).items()}
        album_tags['id'] = str(album_id)
        album_tags['quality'] = f' [{album_info.quality}]' if album_info.quality else ''
        album_tags['explicit'] = ' [E]' if album_info.explicit else ''
        album_tags['artist_initials'] = self._get_artist_initials_from_name(album_info)

        # album_path = path + self.global_settings['formatting']['album_format'].format(**album_tags) # OLD
        album_path_formatted_name = self.global_settings['formatting']['album_format'].format(**album_tags)
        album_path = os.path.join(path, album_path_formatted_name)
        # fix path byte limit
        album_path = fix_byte_limit(album_path) + '/'
        os.makedirs(album_path, exist_ok=True)

        return album_path

    def _create_track_location(self, album_location: str, track_info: TrackInfo) -> str:
        """Create the full file path for a track"""
        # Clean up track tags and add special formats
        track_tags = {k: sanitise_name(v) for k, v in asdict(track_info).items()}
        track_tags['explicit'] = ' [E]' if track_info.explicit else ''
        
        # Add commonly used format variables
        track_tags['artist'] = ', '.join([sanitise_name(artist) for artist in track_info.artists]) if track_info.artists else ''
        track_tags['album_artist'] = sanitise_name(track_info.tags.album_artist) if track_info.tags.album_artist else track_tags['artist']
        
        # Add commonly used tag fields from track_info.tags
        track_tags['isrc'] = sanitise_name(track_info.tags.isrc) if track_info.tags.isrc else ''
        track_tags['upc'] = sanitise_name(track_info.tags.upc) if track_info.tags.upc else ''
        track_tags['composer'] = sanitise_name(track_info.tags.composer) if track_info.tags.composer else ''
        track_tags['label'] = sanitise_name(track_info.tags.label) if track_info.tags.label else ''
        track_tags['release_date'] = track_info.tags.release_date if track_info.tags.release_date else ''
        track_tags['genres'] = ', '.join(track_info.tags.genres) if track_info.tags.genres else ''
        
        # Add all documented format variables from GUI with default values
        track_tags['track_number'] = str(track_info.tags.track_number) if track_info.tags.track_number else ''
        track_tags['total_tracks'] = str(track_info.tags.total_tracks) if track_info.tags.total_tracks else ''
        track_tags['disc_number'] = str(track_info.tags.disc_number) if track_info.tags.disc_number else ''
        track_tags['total_discs'] = str(track_info.tags.total_discs) if track_info.tags.total_discs else ''
        track_tags['quality'] = track_info.codec.name if track_info.codec else ''
        track_tags['artist_initials'] = self._get_artist_initials_from_name(AlbumInfo(name='', artist=track_tags['artist'], tracks=[], release_year=0))

        # Add aliases for GUI format compatibility (required by default format strings)
        track_tags['track_name'] = track_tags.get('name', track_info.name if track_info.name else '')
        track_tags['track_artist'] = track_tags.get('artist', '')
        
        # Handle track/disc number formatting with zero-fill if enabled
        if self.global_settings['formatting']['enable_zfill']:
            if track_info.tags.track_number and track_info.tags.total_tracks:
                total_digits = len(str(track_info.tags.total_tracks))
                track_tags['track_number'] = str(track_info.tags.track_number).zfill(total_digits)
            if track_info.tags.disc_number and track_info.tags.total_discs:
                total_digits = len(str(track_info.tags.total_discs))
                track_tags['disc_number'] = str(track_info.tags.disc_number).zfill(total_digits)
        
        # Get the appropriate format string
        # Better detection for single track downloads
        is_single_track_download = (
            album_location == self.path or  # Original condition (CLI and proper single tracks)
            (hasattr(self, 'download_mode') and self.download_mode is DownloadTypeEnum.track)  # Track download mode
        )
        
        if is_single_track_download:
            format_string = self.global_settings['formatting']['single_full_path_format']
        else:  # Track in album/playlist
            format_string = self.global_settings['formatting']['track_filename_format']
        
        # Format the filename
        track_filename = format_string.format(**track_tags)
        
        # Add file extension based on codec
        codec_extensions = {
            CodecEnum.FLAC: '.flac',
            CodecEnum.MP3: '.mp3',
            CodecEnum.AAC: '.m4a',
            CodecEnum.ALAC: '.m4a',
            CodecEnum.OPUS: '.opus',
            CodecEnum.VORBIS: '.ogg',
            CodecEnum.WAV: '.wav',
            CodecEnum.AIFF: '.aiff',
            CodecEnum.AC4: '.ac4',
            CodecEnum.AC3: '.ac3',
            CodecEnum.EAC3: '.eac3'
        }
        extension = codec_extensions.get(track_info.codec, '.flac')  # Default to .flac
        track_filename += extension
        
        # Combine with album location
        track_location = os.path.join(album_location, track_filename)
        
        # Fix byte limit
        track_location = fix_byte_limit(track_location)
        
        return track_location

    def _download_album_files(self, album_path: str, album_info: AlbumInfo):
        if album_info.cover_url and self.global_settings['covers']['save_external']:
            download_file(album_info.cover_url, f'{album_path}cover.{album_info.cover_type.name}', artwork_settings=self._get_artwork_settings())

        if album_info.animated_cover_url and self.global_settings['covers']['save_animated_cover']:
            self.print('Downloading animated album cover')
            download_file(album_info.animated_cover_url, album_path + 'cover.mp4', enable_progress_bar=self.global_settings['general'].get('progress_bar', True))

        if album_info.description:
            with open(album_path + 'description.txt', 'w', encoding='utf-8') as f:
                f.write(album_info.description)  # Also add support for this with singles maybe?

    def download_album(self, album_id, artist_name='', path=None, indent_level=1, extra_kwargs=None):
        # Set indent
        self.set_indent_number(indent_level)
        d_print = self.oprinter.oprint
        symbols = self._get_status_symbols()

        # Get album info - use indent level 1 to match album details
        self.set_indent_number(1)
        self.print(f'Fetching data. Please wait...')
        album_info: AlbumInfo = self.service.get_album_info(album_id, **(extra_kwargs or {}))

        if not album_info:
            logging.warning(f"Could not retrieve album info for {album_id} from {self.service_name}. Skipping album.")
            return []
        
        number_of_tracks = len(album_info.tracks)
        path = self.path if not path else path

        if number_of_tracks > 1 or self.global_settings['formatting']['force_album_format']:
            # Creates the album_location folders
            album_path = self._create_album_location(path, album_id, album_info)
        
            if self.download_mode is DownloadTypeEnum.album:
                self.set_indent_number(1)
                self.print(f'=== Downloading album {album_info.name} ({album_id}) ===', drop_level=1)
            elif self.download_mode is DownloadTypeEnum.artist:
                self.set_indent_number(1)
                self.print(f'=== Downloading album {album_info.name} ({album_id}) ===', drop_level=1)
            self.print(f'Artist: {album_info.artist} ({album_info.artist_id})')
            if album_info.release_year: self.print(f'Year: {album_info.release_year}')
            if album_info.duration: self.print(f'Duration: {beauty_format_seconds(album_info.duration)}')
            self.print(f'Number of tracks: {number_of_tracks!s}')
            colored_platform = get_colored_platform_name(self.module_settings[self.service_name].service_name)
            self.print(f'Platform: {colored_platform}')

            if album_info.booklet_url and not os.path.exists(album_path + 'Booklet.pdf'):
                self.print('Downloading booklet')
                download_file(album_info.booklet_url, album_path + 'Booklet.pdf')
            
            cover_temp_location = download_to_temp(album_info.all_track_cover_jpg_url) if album_info.all_track_cover_jpg_url else ''

            # Download booklet, animated album cover and album cover if present
            self._download_album_files(album_path, album_info)

            # Get concurrent downloads setting
            concurrent_downloads = self.global_settings['general'].get('concurrent_downloads', 1)
            
            # Force sequential downloads for specific modules or when concurrent_downloads is 1
            service_name_lower = ""
            if hasattr(self, 'service_name') and self.service_name:
                service_name_lower = self.service_name.lower()
            
            # Check if sequential downloads should be forced
            force_sequential = False
            sequential_reason = ""
            
            if concurrent_downloads == 1:
                force_sequential = True
                sequential_reason = "concurrent_downloads setting is 1"
            elif service_name_lower == 'spotify':
                force_sequential = True
                sequential_reason = "Spotify (rate limiting protection)"
            elif service_name_lower == 'applemusic':
                force_sequential = True
                sequential_reason = "Apple Music"
            
            if force_sequential:
                concurrent_downloads = 1
                self.print(f"Using sequential downloads for {sequential_reason}")
            
            if concurrent_downloads > 1 and number_of_tracks > 1:
                # Prepare download arguments for all tracks
                download_args_list = []
                for index, track_item in enumerate(album_info.tracks, start=1):
                    track_id_to_download = track_item.id if hasattr(track_item, 'id') else track_item
                    
                    # For artist downloads, check if we're processing album tracks (indent_level > 1) or individual tracks
                    # For regular album downloads, use indent level 1 (8 spaces) for track content
                    if self.download_mode is DownloadTypeEnum.artist:
                        # If indent_level > 1, we're processing album tracks within artist download, use level 1 (8 spaces)
                        # If indent_level == 1, we're processing individual artist tracks, use level 0 (no indent)
                        track_content_indent = 1 if indent_level > 1 else 0
                    else:
                        track_content_indent = 1
                    download_args = {
                        'track_id': track_id_to_download,
                        'album_location': album_path,
                        'track_index': index,
                        'number_of_tracks': number_of_tracks,
                        'main_artist': artist_name,
                        'cover_temp_location': cover_temp_location,
                        'indent_level': track_content_indent,
                        'extra_kwargs': album_info.track_extra_kwargs
                    }
                    download_args_list.append(download_args)
                
                # Download tracks concurrently
                results = self._concurrent_download_tracks(album_info.tracks, download_args_list, concurrent_downloads, performance_summary_indent=0)
                
                # Process results and collect rate-limited tracks
                # (Errors are already reported by concurrent download progress monitor)
                rate_limited_tracks = []
                for index, (original_index, result, error) in enumerate(results):
                    if error and result == "RATE_LIMITED":
                        track_item = album_info.tracks[original_index]
                        track_id_to_download = track_item.id if hasattr(track_item, 'id') else track_item
                        rate_limited_tracks.append({
                            'id': track_id_to_download,
                            'extra_kwargs': album_info.track_extra_kwargs,
                            'original_index': original_index + 1,
                            'track_item': track_item
                        })
                    elif result == "RATE_LIMITED":
                        track_item = album_info.tracks[original_index]
                        track_id_to_download = track_item.id if hasattr(track_item, 'id') else track_item
                        rate_limited_tracks.append({
                            'id': track_id_to_download,
                            'extra_kwargs': album_info.track_extra_kwargs,
                            'original_index': original_index + 1,
                            'track_item': track_item
                        })
                
                # Retry rate-limited tracks for Spotify and Apple Music
                if rate_limited_tracks and service_name_lower in ['spotify', 'applemusic']:
                    self.set_indent_number(indent_level + 1)
                    print()  # Add spacing before retry section
                    if service_name_lower == 'applemusic':
                        self.print(f'{len(rate_limited_tracks)} tracks failed with temporary errors. Retrying...', drop_level=1)
                        self.print("Using sequential downloads for Apple Music retries", drop_level=1)
                    else:
                        self.print(f'{len(rate_limited_tracks)} tracks deferred due to rate limiting. Retrying...', drop_level=1)
                    
                    for i, retry_item in enumerate(rate_limited_tracks):
                        # For artist downloads, keep track headers at level 2; for regular albums, use level 1
                        track_indent_level = 2 if self.download_mode is DownloadTypeEnum.artist else 1
                        self.set_indent_number(track_indent_level)
                        print()  # Spacing
                        # Track headers should be indented (8 spaces) in regular album downloads, no drop for artist downloads
                        drop_level_for_retry_track = 1 if self.download_mode is DownloadTypeEnum.artist else 0
                        self.print(f'Track {retry_item["original_index"]}/{number_of_tracks} (Retry Pass)', drop_level=drop_level_for_retry_track)
                        # For artist downloads, check if we're processing album tracks (indent_level > 1) or individual tracks  
                        # For regular album downloads, use indent level 1 (8 spaces) for track content
                        if self.download_mode is DownloadTypeEnum.artist:
                            # If indent_level > 1, we're processing album tracks within artist download, use level 1 (8 spaces)
                            # If indent_level == 1, we're processing individual artist tracks, use level 0 (no indent)
                            track_content_indent = 1 if indent_level > 1 else 0
                        else:
                            track_content_indent = 1
                        self.download_track(
                            retry_item['id'],
                            album_location=album_path,
                            track_index=retry_item["original_index"],
                            number_of_tracks=number_of_tracks,
                            main_artist=artist_name,
                            cover_temp_location=cover_temp_location,
                            indent_level=track_content_indent,
                            extra_kwargs=retry_item['extra_kwargs']
                        )
                        # Add pause between retry tracks (except for the last one)
                        if i < len(rate_limited_tracks) - 1:
                            print()
                            if service_name_lower == 'applemusic':
                                self.print("Pausing 2 seconds before retry...", drop_level=1)
                                time.sleep(2)
                            else:
                                self.print("Pausing 30 seconds to prevent rate limiting...", drop_level=1)
                                time.sleep(30)
                else:
                    # Only show rate limiting message for Spotify (where it's relevant)
                    if service_name_lower == 'spotify':
                        # Force rate limiting message to have exactly 8 spaces indentation
                        current_indent = self.indent_number
                        self.set_indent_number(1)
                        self.print("No tracks were deferred due to rate limiting.")
                        self.set_indent_number(current_indent)
                        print()  # Add blank line after message
            else:
                # Fallback to sequential downloads
                rate_limited_tracks = []  # Initialize list for deferred tracks
                
                for index, track_item in enumerate(album_info.tracks, start=1):
                    # For artist downloads, keep track headers at level 2; for regular albums, use level 1
                    track_indent_level = 2 if self.download_mode is DownloadTypeEnum.artist else 1
                    self.set_indent_number(track_indent_level)
                    # Track headers should be indented (8 spaces) in regular album downloads, no drop for artist downloads
                    drop_level_for_track = 1 if self.download_mode is DownloadTypeEnum.artist else 0
                    # Only show "Pass 1" for Spotify (which has retry passes)
                    pass_indicator = " (Pass 1)" if service_name_lower == 'spotify' else ""
                    self.print(f'Track {index}/{number_of_tracks}{pass_indicator}', drop_level=drop_level_for_track)
                    track_id_to_download = track_item.id if hasattr(track_item, 'id') else track_item # Check for .id attribute
                    # For artist downloads, check if we're processing album tracks (indent_level > 1) or individual tracks
                    # For regular album downloads, use indent level 1 (8 spaces) for track content
                    if self.download_mode is DownloadTypeEnum.artist:
                        # If indent_level > 1, we're processing album tracks within artist download, use level 1 (8 spaces)
                        # If indent_level == 1, we're processing individual artist tracks, use level 0 (no indent)
                        track_content_indent = 1 if indent_level > 1 else 0
                    else:
                        track_content_indent = 1
                    download_result = self.download_track(track_id_to_download, album_location=album_path, track_index=index, number_of_tracks=number_of_tracks, main_artist=artist_name, cover_temp_location=cover_temp_location, indent_level=track_content_indent, extra_kwargs=album_info.track_extra_kwargs)
                    
                    # Add pause between downloads for Spotify to prevent rate limiting
                    # Only pause if track was actually downloaded (not skipped) and not the last track
                    if (service_name_lower == 'spotify' and index < number_of_tracks and 
                        download_result is not None and download_result != "RATE_LIMITED"):
                        pause_seconds = self._get_spotify_pause_seconds()
                        self.print(f'Pausing {pause_seconds} seconds to prevent rate limiting...', drop_level=1)
                        time.sleep(pause_seconds)
                        print()  # Add blank line after pause message for consistent spacing with playlists
                    
                    # Collect rate-limited tracks for retry
                    if download_result == "RATE_LIMITED":
                        logging.info(f"Deferring album track {track_id_to_download} due to rate limit.")
                        rate_limited_tracks.append({
                            'id': track_id_to_download,
                            'extra_kwargs': album_info.track_extra_kwargs,
                            'original_index': index,
                            'track_item': track_item
                        })
                
                # Retry rate-limited tracks for Spotify and Apple Music
                if rate_limited_tracks and service_name_lower in ['spotify', 'applemusic']:
                    self.set_indent_number(indent_level + 1)
                    print()  # Add spacing before retry section
                    if service_name_lower == 'applemusic':
                        self.print(f'{len(rate_limited_tracks)} tracks failed with temporary errors. Retrying...', drop_level=1)
                        self.print("Using sequential downloads for Apple Music retries", drop_level=1)
                    else:
                        self.print(f'{len(rate_limited_tracks)} tracks deferred due to rate limiting. Retrying...', drop_level=1)
                    
                    for i, retry_item in enumerate(rate_limited_tracks):
                        # For artist downloads, keep track headers at level 2; for regular albums, use level 1
                        track_indent_level = 2 if self.download_mode is DownloadTypeEnum.artist else 1
                        self.set_indent_number(track_indent_level)
                        print()  # Spacing
                        # Track headers should be indented (8 spaces) in regular album downloads, no drop for artist downloads
                        drop_level_for_retry_track_seq = 1 if self.download_mode is DownloadTypeEnum.artist else 0
                        self.print(f'Track {retry_item["original_index"]}/{number_of_tracks} (Retry Pass)', drop_level=drop_level_for_retry_track_seq)
                        # For artist downloads, check if we're processing album tracks (indent_level > 1) or individual tracks
                        # For regular album downloads, use indent level 1 (8 spaces) for track content
                        if self.download_mode is DownloadTypeEnum.artist:
                            # If indent_level > 1, we're processing album tracks within artist download, use level 1 (8 spaces)
                            # If indent_level == 1, we're processing individual artist tracks, use level 0 (no indent)
                            track_content_indent = 1 if indent_level > 1 else 0
                        else:
                            track_content_indent = 1
                        self.download_track(
                            retry_item['id'],
                            album_location=album_path,
                            track_index=retry_item["original_index"],
                            number_of_tracks=number_of_tracks,
                            main_artist=artist_name,
                            cover_temp_location=cover_temp_location,
                            indent_level=track_content_indent,
                            extra_kwargs=retry_item['extra_kwargs']
                        )
                        # Add pause between retry tracks (except for the last one)
                        if i < len(rate_limited_tracks) - 1:
                            print()
                            if service_name_lower == 'applemusic':
                                self.print("Pausing 2 seconds before retry...", drop_level=1)
                                time.sleep(2)
                            else:
                                self.print("Pausing 30 seconds to prevent rate limiting...", drop_level=1)
                                time.sleep(30)
                else:
                    # Only show rate limiting message for Spotify (where it's relevant)
                    if service_name_lower == 'spotify':
                        # Force rate limiting message to have exactly 8 spaces indentation
                        current_indent = self.indent_number
                        self.set_indent_number(1)
                        self.print("No tracks were deferred due to rate limiting.")
                        self.set_indent_number(current_indent)
                        print()  # Add blank line after message

            # For artist downloads, align album completion with album start message
            if self.download_mode is DownloadTypeEnum.artist:
                self.set_indent_number(1)  # Same as album start for artist downloads
            else:
                self.set_indent_number(indent_level)
            symbols = self._get_status_symbols()
            self.print(f'=== {symbols["success"]} Album completed ===', drop_level=1)
            # Add 2 empty lines after album completion for visual separation
            print()
            print()
            if cover_temp_location: silentremove(cover_temp_location)
        elif number_of_tracks == 1:
            # Single-track albums go directly to track download without album header or completion message
            single_track_item = album_info.tracks[0]
            track_id_to_download = single_track_item.id if hasattr(single_track_item, 'id') else single_track_item # Check for .id attribute
            self.download_track(track_id_to_download, album_location=path, number_of_tracks=1, main_artist=artist_name, indent_level=indent_level, extra_kwargs=album_info.track_extra_kwargs)

        return album_info.tracks

    def download_artist(self, artist_id, extra_kwargs=None):        
        # Start with a copy of extra_kwargs if provided, or an empty dict
        prepared_kwargs = {} 
        if extra_kwargs:
            prepared_kwargs.update(extra_kwargs)

        service_name_lower = ""
        if hasattr(self, 'service_name') and self.service_name:
            service_name_lower = self.service_name.lower()

        # Specific kwarg handling for Beatport/Beatsource for the 'data' key
        if service_name_lower in ['beatport', 'beatsource']:
            if 'data' in prepared_kwargs:
                logging.debug(f"Popping 'data' kwarg for {self.service_name}.get_artist_info as it is unexpected.")
                prepared_kwargs.pop('data', None)

        # Determine the value for fetching credited albums from global settings        
        fetch_credited_albums_value = False
        if (
            'artist_downloading' in self.global_settings and
            isinstance(self.global_settings['artist_downloading'], dict) and
            'return_credited_albums' in self.global_settings['artist_downloading']
        ):
            fetch_credited_albums_value = self.global_settings['artist_downloading']['return_credited_albums']

        # Call get_artist_info based on service-specific signature requirements
        try:
            if service_name_lower in ['deezer', 'qobuz', 'soundcloud', 'tidal', 'beatport', 'beatsource']:
                # These services require 'get_credited_albums' (the boolean value) as the second positional argument.            
                artist_info: ArtistInfo = self.service.get_artist_info(artist_id, fetch_credited_albums_value, **prepared_kwargs)
            elif service_name_lower == 'spotify':
                # Spotify handles 'return_credited_albums' as a keyword argument.
                prepared_kwargs['return_credited_albums'] = fetch_credited_albums_value
                artist_info: ArtistInfo = self.service.get_artist_info(artist_id, **prepared_kwargs)
            else:
                # For any other unhandled services.
                # Assume they don't need 'get_credited_albums' positionally or as a specific keyword.
                # This branch may need refinement if other services show different signature needs.
                artist_info: ArtistInfo = self.service.get_artist_info(artist_id, **prepared_kwargs)
        except Exception as e:
            self.print(f"Failed to retrieve artist info for ID {artist_id}: {e}", drop_level=1)
            symbols = self._get_status_symbols()
            self.print(f"=== {symbols['error']} Artist failed ===", drop_level=1)
            return

        # Check if artist_info is None (some services may return None instead of raising an exception)
        if artist_info is None:
            self.print(f"Failed to retrieve artist info for ID {artist_id}: Service returned None", drop_level=1)
            symbols = self._get_status_symbols()
            self.print(f"=== {symbols['error']} Artist failed ===", drop_level=1)
            return

        artist_name = artist_info.name

        self.set_indent_number(1)

        number_of_albums = len(artist_info.albums)
        number_of_tracks = len(artist_info.tracks)

        self.print(f'=== Downloading artist {artist_name} ({artist_id}) ===', drop_level=1)
        if number_of_albums: self.print(f'Number of albums: {number_of_albums!s}')
        if number_of_tracks: self.print(f'Number of tracks: {number_of_tracks!s}')
        colored_platform = get_colored_platform_name(self.module_settings[self.service_name].service_name)
        self.print(f'Platform: {colored_platform}')
        artist_path = os.path.join(self.path, sanitise_name(artist_name)) + '/'
        
        # Create the artist directory if it doesn't exist
        os.makedirs(artist_path, exist_ok=True)

        tracks_downloaded = []
        for index, album_item in enumerate(artist_info.albums, start=1):
            # Ensure consistent indentation for Album headers (8 spaces)
            self.set_indent_number(1)
            self.print(f'Album {index}/{number_of_albums}')

            album_id_to_process = None
            # Check if album_item is a string (like for Tidal)
            if isinstance(album_item, str):
                album_id_to_process = album_item
            # Check if album_item is a dictionary with an 'id' key (like for Spotify)
            elif isinstance(album_item, dict) and 'id' in album_item and isinstance(album_item['id'], str):
                album_id_to_process = album_item['id']
            # Check if album_item is an object with an 'id' attribute (more generic)
            elif hasattr(album_item, 'id') and isinstance(getattr(album_item, 'id', None), str):
                 album_id_to_process = album_item.id # type: ignore
            else:
                self.print(f"Skipping unrecognized album item in artist_info.albums: {album_item}")
                continue
            
            tracks_downloaded += self.download_album(
                album_id_to_process, # This is now guaranteed to be a string ID
                artist_name=artist_name,
                path=artist_path,
                indent_level=2,
                extra_kwargs=artist_info.album_extra_kwargs # General extra_kwargs from artist level
            )

        self.set_indent_number(2)
        skip_tracks = self.global_settings['artist_downloading']['separate_tracks_skip_downloaded']
        tracks_to_download = [i for i in artist_info.tracks if (i not in tracks_downloaded and skip_tracks) or not skip_tracks]
        number_of_tracks_new = len(tracks_to_download)
        
        if number_of_tracks_new > 0:
            
            # Get concurrent downloads setting
            concurrent_downloads = self.global_settings['general'].get('concurrent_downloads', 1)
            
            # Force sequential downloads for Spotify due to rate limiting
            # Limit Apple Music to 3 concurrent downloads for I/O stability
            service_name_lower = ""
            if hasattr(self, 'service_name') and self.service_name:
                service_name_lower = self.service_name.lower()
            
            if service_name_lower == 'spotify':
                concurrent_downloads = 1
                print()  # Add blank line before sequential downloads message
                self.print("Using sequential downloads for Spotify (rate limiting protection)", drop_level=1)
            elif service_name_lower == 'applemusic':
                concurrent_downloads = 1
                print()  # Add blank line before sequential downloads message
                self.print("Using sequential downloads for Apple Music", drop_level=1)
            
            if concurrent_downloads > 1 and number_of_tracks_new > 1:
                
                # Prepare download arguments for all tracks
                download_args_list = []
                for index, track_id in enumerate(tracks_to_download, start=1):
                    download_args = {
                        'track_id': track_id,
                        'album_location': artist_path,
                        'main_artist': artist_name,
                        'number_of_tracks': 1,  # Each track is individual for artist downloads
                        'indent_level': 1,
                        'extra_kwargs': artist_info.track_extra_kwargs
                    }
                    download_args_list.append(download_args)
                
                # Download tracks concurrently
                results = self._concurrent_download_tracks(tracks_to_download, download_args_list, concurrent_downloads, performance_summary_indent=1)
                
                # Process results and collect rate-limited tracks
                # (Errors are already reported by concurrent download progress monitor)
                rate_limited_tracks = []
                for index, (original_index, result, error) in enumerate(results):
                    if error and result == "RATE_LIMITED":
                        track_id = tracks_to_download[original_index]
                        rate_limited_tracks.append({
                            'id': track_id,
                            'extra_kwargs': artist_info.track_extra_kwargs,
                            'original_index': original_index + 1
                        })
                    elif result == "RATE_LIMITED":
                        track_id = tracks_to_download[original_index]
                        rate_limited_tracks.append({
                            'id': track_id,
                            'extra_kwargs': artist_info.track_extra_kwargs,
                            'original_index': original_index + 1
                        })
                
                # Retry rate-limited tracks for Spotify and Apple Music
                if rate_limited_tracks and service_name_lower in ['spotify', 'applemusic']:
                    print()  # Add spacing before retry section
                    self.print(f'{len(rate_limited_tracks)} tracks deferred due to rate limiting. Retrying...', drop_level=1)
                    
                    for i, retry_item in enumerate(rate_limited_tracks):
                        print()  # Spacing
                        self.print(f'Track {retry_item["original_index"]}/{number_of_tracks_new} (Retry Pass)', drop_level=1)
                        self.download_track(
                            retry_item['id'],
                            album_location=artist_path,
                            main_artist=artist_name,
                            number_of_tracks=1,
                            indent_level=1,
                            extra_kwargs=retry_item['extra_kwargs']
                        )
                        # Add 30-second pause between retry tracks (except for the last one)
                        if i < len(rate_limited_tracks) - 1:
                            print()
                            self.print("Pausing 30 seconds to prevent rate limiting...", drop_level=1)
                            time.sleep(30)
                else:
                    # Only show rate limiting message for Spotify (where it's relevant)
                    if service_name_lower == 'spotify':
                        self.print("        No tracks were deferred due to rate limiting.")
            else:
                # Fallback to sequential downloads
                rate_limited_tracks = []  # Initialize list for deferred tracks
                
                for index, track_id in enumerate(tracks_to_download, start=1):
                    print()  # Add blank line before each track in artist downloads
                    # Only show "Pass 1" for Spotify (which has retry passes)
                    pass_indicator = " (Pass 1)" if service_name_lower == 'spotify' else ""
                    self.print(f'Track {index}/{number_of_tracks_new}{pass_indicator}', drop_level=1)
                    download_result = self.download_track(track_id, album_location=artist_path, main_artist=artist_name, number_of_tracks=1, indent_level=1, extra_kwargs=artist_info.track_extra_kwargs)
                    
                    # Add pause between downloads for Spotify to prevent rate limiting
                    # Only pause if track was actually downloaded (not skipped) and not the last track
                    if (service_name_lower == 'spotify' and index < number_of_tracks_new and 
                        download_result is not None and download_result != "RATE_LIMITED"):
                        pause_seconds = self._get_spotify_pause_seconds()
                        self.print(f'Pausing {pause_seconds} seconds to prevent rate limiting...', drop_level=1)
                        time.sleep(pause_seconds)
                        print()  # Add blank line after pause message for consistent spacing
                    
                    # Collect rate-limited tracks for retry
                    if download_result == "RATE_LIMITED":
                        logging.info(f"Deferring artist track {track_id} due to rate limit.")
                        rate_limited_tracks.append({
                            'id': track_id,
                            'extra_kwargs': artist_info.track_extra_kwargs,
                            'original_index': index
                        })
                
                # Retry rate-limited tracks for Spotify
                if rate_limited_tracks and service_name_lower == 'spotify':
                    print()  # Add spacing before retry section
                    self.print(f'{len(rate_limited_tracks)} tracks deferred due to rate limiting. Retrying...', drop_level=1)
                    
                    for i, retry_item in enumerate(rate_limited_tracks):
                        print()  # Spacing
                        self.print(f'Track {retry_item["original_index"]}/{number_of_tracks_new} (Retry Pass)', drop_level=1)
                        self.download_track(
                            retry_item['id'],
                            album_location=artist_path,
                            main_artist=artist_name,
                            number_of_tracks=1,
                            indent_level=1,
                            extra_kwargs=retry_item['extra_kwargs']
                        )
                        # Add 30-second pause between retry tracks (except for the last one)
                        if i < len(rate_limited_tracks) - 1:
                            print()
                            self.print("Pausing 30 seconds to prevent rate limiting...", drop_level=1)
                            time.sleep(30)
                else:
                    # Only show rate limiting message for Spotify (where it's relevant)
                    if service_name_lower == 'spotify':
                        print()  # Add blank line before message
                        self.print("        No tracks were deferred due to rate limiting.")
                        # Don't add blank line after message - let track completion handle spacing

        self.set_indent_number(1)
        tracks_skipped = number_of_tracks - number_of_tracks_new
        if tracks_skipped > 0: self.print(f'Tracks skipped: {tracks_skipped!s}', drop_level=1)
        symbols = self._get_status_symbols()
        self.print(f'=== {symbols["success"]} Artist completed ===', drop_level=1)
        # Add 2 empty lines after artist completion for visual separation
        print()
        print()

    async def _download_track_async(self, session, track_id=None, track_info=None, download_info=None, album_location='', main_artist='', track_index=0, number_of_tracks=0, cover_temp_location='', indent_level=1, m3u_playlist=None, extra_kwargs={}, verbose=True):
        """Async version of download_track for use with concurrent downloads - OPTIMIZED VERSION"""
        import os
        import shutil
        from utils.utils import download_file_async
        from utils.models import QualityEnum, CodecOptions, DownloadEnum, ContainerEnum, CodecEnum
        from orpheus.tagging import tag_file
        import asyncio
        
        # If track_info and download_info are not provided, fetch them (fallback for compatibility)
        if track_info is None or download_info is None:
            if track_id is None:
                return None
                
            # Get event loop for async file operations
            loop = asyncio.get_event_loop()
                
            # Check if track already exists
            if album_location == '' and await loop.run_in_executor(None, os.path.isfile, track_id):
                return None
                
            # Get track info and download info (fallback - should not be used in optimized path)
            try:
                quality_tier = QualityEnum[self.global_settings['general']['download_quality'].upper()]
                codec_options = CodecOptions(
                    spatial_codecs = self.global_settings['codecs']['spatial_codecs'],
                    proprietary_codecs = self.global_settings['codecs']['proprietary_codecs'],
                )
                
                # Move fallback API calls to thread pool too
                loop = asyncio.get_event_loop()
                
                def get_track_info_fallback():
                    return self.service.get_track_info(track_id, quality_tier, codec_options, **extra_kwargs)
                
                def get_download_info_fallback(track_info_for_download):
                    # Check if track_info has download_extra_kwargs (like Qobuz, TIDAL)
                    if hasattr(track_info_for_download, 'download_extra_kwargs') and track_info_for_download.download_extra_kwargs:
                        return self.service.get_track_download(**track_info_for_download.download_extra_kwargs)
                    else:
                        # Try the full signature first (for modules that support it)
                        try:
                            return self.service.get_track_download(track_id, quality_tier, codec_options, **extra_kwargs)
                        except TypeError:
                            # Fallback for modules with simpler signatures
                            return self.service.get_track_download(track_id, quality_tier)
                
                # First get track info
                track_info = await loop.run_in_executor(None, get_track_info_fallback)
                
                # Check if file already exists BEFORE getting download info (for temp file modules like Deezer)
                if track_info:
                    track_location = self._create_track_location(album_location, track_info)
                    if await loop.run_in_executor(None, os.path.isfile, track_location):
                        return "ALREADY_EXISTS"
                
                # Then get download info using the track_info
                download_info = await loop.run_in_executor(None, get_download_info_fallback, track_info)
            except Exception as e:
                return None
                
        if not track_info or not download_info:
            return None
            
        # Extract track_id from track_info if not provided
        if track_id is None:
            track_id = track_info.id
            
        # Check if track already exists (for backward compatibility) - use thread pool for file checks
        loop = asyncio.get_event_loop()
        if album_location == '' and await loop.run_in_executor(None, os.path.isfile, track_id):
            return "ALREADY_EXISTS"
            
        # Create track location
        track_location = self._create_track_location(album_location, track_info)
        
        # Check if file already exists - use thread pool for file checks
        if await loop.run_in_executor(None, os.path.isfile, track_location):
            return "ALREADY_EXISTS"
            
        # Download the audio file
        try:
            if download_info.download_type is DownloadEnum.URL:
                result_tuple = await download_file_async(
                    session,
                    download_info.file_url,
                    track_location,
                    headers=download_info.file_url_headers,
                    enable_progress_bar=False,  # Disable progress bar for concurrent downloads
                    indent_level=0
                )
                # Extract file location and bytes downloaded
                if isinstance(result_tuple, tuple):
                    final_location, bytes_downloaded = result_tuple
                else:
                    # Fallback for old return format
                    final_location = result_tuple
                    bytes_downloaded = 0
            else:
                # For non-URL downloads, fall back to synchronous method using thread pool
                loop = asyncio.get_event_loop()
                final_location = await loop.run_in_executor(None, shutil.move, download_info.temp_file_path, track_location)
                # Get file size for non-URL downloads using thread pool
                try:
                    bytes_downloaded = await loop.run_in_executor(None, os.path.getsize, final_location)
                except OSError:
                    bytes_downloaded = 0
        except Exception as e:
            return None
            
        if not final_location:
            return None
            
        # Validate file size to catch corrupted downloads - use thread pool for file operations
        try:
            loop = asyncio.get_event_loop()
            file_size = await loop.run_in_executor(None, os.path.getsize, final_location)
            min_file_size = 100 * 1024  # 100KB threshold
            
            if file_size < min_file_size:
                try:
                    await loop.run_in_executor(None, os.remove, final_location)
                except:
                    pass
                return None
        except OSError:
            pass  # Continue if size check fails
            
        # Download artwork asynchronously only if needed (for embedding or external saving)
        artwork_path = ''
        needs_artwork = (self.global_settings['covers']['embed_cover'] or 
                        self.global_settings['covers']['save_external'])
        
        if track_info.cover_url and needs_artwork:
            try:
                artwork_path = self.create_temp_filename()
                artwork_result = await download_file_async(
                    session,
                    track_info.cover_url, 
                    artwork_path, 
                    artwork_settings=self._get_artwork_settings(),
                    enable_progress_bar=False,
                    indent_level=0
                )
                # Handle new return format for artwork download
                if isinstance(artwork_result, tuple):
                    artwork_path, _ = artwork_result  # We don't need bytes for artwork
                else:
                    artwork_path = artwork_result
            except Exception:
                artwork_path = ''  # Continue without artwork if download fails
        
        # Do conversion BEFORE tagging (like old version) - run in thread pool
        loop = asyncio.get_event_loop()
        conversion_result = await loop.run_in_executor(
            None,
            self._convert_file_if_needed,
            final_location,
            track_info,
            lambda msg: None  # Dummy print function for async context
        )
        converted_location, old_track_location, old_container = conversion_result
        if converted_location and converted_location != final_location:
            final_location = converted_location
                
        # Tag file using thread pool to avoid blocking async event loop (after conversion)
        try:
            # Determine container from actual file extension (after potential conversion)
            file_extension = os.path.splitext(final_location)[1].lower()
            container_map = {
                '.flac': ContainerEnum.flac,
                '.mp3': ContainerEnum.mp3,
                '.m4a': ContainerEnum.m4a,
                '.opus': ContainerEnum.opus,
                '.ogg': ContainerEnum.ogg,
                '.wav': ContainerEnum.wav,
                '.aiff': ContainerEnum.aiff,
                '.ac4': ContainerEnum.ac4,
                '.ac3': ContainerEnum.ac3,
                '.eac3': ContainerEnum.eac3
            }
            container = container_map.get(file_extension, ContainerEnum.flac)
            
            
            # Get embedded lyrics if available
            embedded_lyrics = getattr(track_info, 'lyrics', None) or ''
            
            # Get credits list (empty for now)
            credits_list = []
            
            # Check if container supports tagging
            tagging_supported_containers = [ContainerEnum.flac, ContainerEnum.mp3, ContainerEnum.m4a, ContainerEnum.ogg]
            
            if container in tagging_supported_containers:
                # Tag the converted file - only pass artwork_path if embed_cover is enabled
                embed_artwork_path = artwork_path if self.global_settings['covers']['embed_cover'] else None
                tag_file(final_location, embed_artwork_path, track_info, credits_list, embedded_lyrics, container)
            else:
                pass  # Skip tagging for unsupported containers like WAV
            
            # Also tag the original file if it was kept (matching old version exactly)
            if old_track_location and old_container:
                if old_container in tagging_supported_containers:
                    embed_artwork_path = artwork_path if self.global_settings['covers']['embed_cover'] else None
                    tag_file(old_track_location, embed_artwork_path, track_info, credits_list, embedded_lyrics, old_container)
                else:
                    pass  # Skip tagging for unsupported containers
            
            # Run m3u playlist addition in thread pool too if needed
            if m3u_playlist:
                await loop.run_in_executor(
                    None,
                    self._add_track_m3u_playlist,
                    m3u_playlist,
                    track_info,
                    final_location
                )
                
            # Clean up temporary artwork file
            if artwork_path and os.path.exists(artwork_path):
                try:
                    os.remove(artwork_path)
                except OSError:
                    pass  # Ignore cleanup errors
            
            # Return tuple with file location and bytes downloaded
            return (final_location, bytes_downloaded)
        except Exception:
            # Clean up temporary artwork file even on failure
            if artwork_path and os.path.exists(artwork_path):
                try:
                    os.remove(artwork_path)
                except OSError:
                    pass  # Ignore cleanup errors
            
            return None  # Return None to indicate failure

    def download_track(self, track_id, album_location='', main_artist='', track_index=0, number_of_tracks=0, cover_temp_location='', indent_level=1, m3u_playlist=None, extra_kwargs={}, verbose=True):
        self.set_indent_number(indent_level)
        # Aliasing for convenience.
        d_print = self.oprinter.oprint
        symbols = self._get_status_symbols()
        track_info: TrackInfo = None
        download_info: TrackDownloadInfo = None
        temp_filename = None

        # Removed: blank line before single track downloads - only add blank line after completion

        # Use a dummy print function when not verbose
        d_print = self.print if verbose else lambda *args, **kwargs: None
        
        # Helper function to return with consistent blank line
        def return_with_blank_line(value):
            # Add blank line after track completion if we're in a multi-track context (album/artist/playlist)
            # Add 2 blank lines for standalone track downloads and single-track albums
            is_standalone_track_download = (hasattr(self, 'download_mode') and 
                                          self.download_mode is DownloadTypeEnum.track and
                                          track_index == 0 and number_of_tracks == 0)
            is_single_track_album = (hasattr(self, 'download_mode') and 
                                   self.download_mode is DownloadTypeEnum.album and 
                                   number_of_tracks == 1)
            is_artist_download = (hasattr(self, 'download_mode') and 
                                self.download_mode is DownloadTypeEnum.artist)
            is_individual_track_in_artist = (is_artist_download and number_of_tracks == 1)
            is_multi_track_download = (hasattr(self, 'download_mode') and 
                                     self.download_mode is DownloadTypeEnum.track and
                                     number_of_tracks > 1)
            is_playlist_download = (hasattr(self, 'download_mode') and 
                                  self.download_mode is DownloadTypeEnum.playlist)
            
            if verbose:
                if (is_standalone_track_download or is_single_track_album or 
                    is_individual_track_in_artist or is_multi_track_download):
                    # Standalone track, single-track album, individual track in artist download, 
                    # or track in multi-track download: add 2 blank lines for better visual separation
                    print()
                    print()
                elif number_of_tracks > 1:
                    if is_playlist_download:
                        # Playlist downloads: add only 1 blank line since playlist logic already adds 1
                        print()
                    else:
                        # Album downloads: add 2 blank lines for better visual separation
                        print()
                        print()
            return value

        quality_tier = QualityEnum[self.global_settings['general']['download_quality'].upper()]
        codec_options = CodecOptions(
            spatial_codecs=self.global_settings['codecs']['spatial_codecs'],
            proprietary_codecs=self.global_settings['codecs']['proprietary_codecs'],
        )

        # Initialize header_drop_level with default value before try block (needed for exception handlers)
        header_drop_level = 1

        # Get track info
        try:
            # Ensure extra_kwargs is always a dictionary
            safe_extra_kwargs = extra_kwargs if extra_kwargs is not None else {}
            track_info: TrackInfo = self.service.get_track_info(track_id, quality_tier, codec_options, **safe_extra_kwargs)
        except Exception as e:
            self.print(f'Could not get track info for {track_id}: {e}')
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
            if isinstance(e, SpotifyRateLimitDetectedError):
                return return_with_blank_line("RATE_LIMITED")
            return return_with_blank_line(None)

        # Check if track_info is None (e.g., episode not found, track unavailable)
        if track_info is None:
            self.print(f'Track info is None for {track_id}. Track may be unavailable or not found.')
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
            return return_with_blank_line(None)

        # For single track downloads, use no indentation for headers but keep indentation for details
        # For multi-track contexts (albums, playlists, artists), use drop_level=1 to align with "Track X/Y" line
        # For single-track albums within artist downloads, use drop_level to remove all indentation
        header_drop_level = 1
        details_indent_adjustment = 0

        # Check if this is a standalone single track download (not part of an album)
        is_standalone_track = (hasattr(self, 'download_mode') and self.download_mode is DownloadTypeEnum.track)

        if number_of_tracks == 1 and not is_standalone_track:
            # This is a single-track album within artist/album downloads, so remove all indentation for track header
            # and ensure track details have exactly 1 level of indentation (8 spaces)
            header_drop_level = indent_level  # Drop back to level 0 (no indentation)
            details_indent_adjustment = 1 - indent_level  # Ensure exactly 1 level of indentation for details
            # Debug: Force 1 level of indentation for single-track album details
            if indent_level == 0:
                details_indent_adjustment = 1  # Force 1 level if starting from 0
        elif is_standalone_track:
            # This is a standalone single track download - use no indentation for header but add indentation for details
            header_drop_level = indent_level  # Drop back to level 0 (no indentation) for header
            details_indent_adjustment = 1 - indent_level  # Ensure exactly 1 level of indentation for details
        elif number_of_tracks > 1:
            # This is a multi-track context (album/artist/playlist) - ensure track details have exactly 1 level of indentation
            details_indent_adjustment = 1 - indent_level  # Adjust to get exactly 1 level of indentation

        d_print(f'=== Downloading track {track_info.name} ({track_id}) ===', drop_level=header_drop_level)

        # Temporarily adjust indent level for track details in single-track albums
        if details_indent_adjustment != 0:
            self.set_indent_number(indent_level + details_indent_adjustment)
        
        # Format and display track information in a user-friendly way
        # Artists with IDs
        if track_info.artists:
            artists_display = ', '.join(track_info.artists)
            if track_info.artist_id:
                d_print(f'Artists: {artists_display} ({track_info.artist_id})')
            else:
                d_print(f'Artists: {artists_display}')
        
        # Release year
        if track_info.release_year:
            d_print(f'Release year: {track_info.release_year}')
        
        # Duration in formatted time
        if track_info.duration:
            formatted_duration = beauty_format_seconds(track_info.duration)
            d_print(f'Duration: {formatted_duration}')
        
        # Platform/Service name
        if self.service_name and hasattr(self.module_settings[self.service_name], 'service_name'):
            colored_platform = get_colored_platform_name(self.module_settings[self.service_name].service_name)
            d_print(f'Platform: {colored_platform}')
        
        # Codec with combined quality information
        codec_info = []
        if track_info.codec:
            codec_name = track_info.codec.name if hasattr(track_info.codec, 'name') else str(track_info.codec).replace('CodecEnum.', '')

            # Special handling for Spotify - show expected values based on quality setting
            if self.service_name.lower() == 'spotify':
                codec_info.append('Codec: VORBIS')

                # Determine bitrate based on quality setting
                quality_setting = self.global_settings['general']['download_quality'].lower()
                if quality_setting in ['lossless', 'hifi', 'high']:
                    codec_info.append('bitrate: 320kbps')
                else:  # low quality
                    codec_info.append('bitrate: 160kbps')

                # Standard values for Spotify
                codec_info.append('bit depth: 16bit')
                codec_info.append('sample rate: 44100kHz')
            else:
                # For other services, use actual track info
                codec_info.append(f'Codec: {codec_name}')

                if track_info.bitrate:
                    codec_info.append(f'bitrate: {track_info.bitrate}kbps')
                if track_info.bit_depth:
                    codec_info.append(f'bit depth: {track_info.bit_depth}bit')
                if track_info.sample_rate:
                    codec_info.append(f'sample rate: {track_info.sample_rate}kHz')

            d_print(', '.join(codec_info))

        # Add track number to tags if it exists
        if track_index: track_info.tags.track_number = track_index
        if number_of_tracks: track_info.tags.total_tracks = number_of_tracks

        # Create track location
        if not album_location:
            # For single track downloads, use the base path
            album_location = self.path
        track_location = self._create_track_location(album_location, track_info)


        if os.path.exists(track_location):
            d_print(f'Track file already exists')
            
            # Restore original indent level if it was adjusted before printing completion message
            if details_indent_adjustment != 0:
                self.set_indent_number(indent_level)
            
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["skip"]} Track skipped ===', drop_level=header_drop_level)

            return return_with_blank_line("SKIPPED")


        # Download lyrics
        if self.global_settings['lyrics']['save_synced_lyrics'] and hasattr(track_info, 'lyrics') and track_info.lyrics:
            d_print('Downloading lyrics')
            with open(f'{os.path.splitext(track_location)[0]}.lrc', 'w', encoding='utf-8') as f:
                f.write(track_info.lyrics)

        # Download audio
        try:
            # Check if track_info has download_extra_kwargs (like TIDAL)
            if hasattr(track_info, 'download_extra_kwargs') and track_info.download_extra_kwargs:
                download_info: TrackDownloadInfo = self.service.get_track_download(**track_info.download_extra_kwargs)
            else:
                # Try the full signature first (for modules that support it)
                # Ensure extra_kwargs is always a dictionary
                safe_extra_kwargs = extra_kwargs if extra_kwargs is not None else {}
                download_info: TrackDownloadInfo = self.service.get_track_download(track_id, quality_tier, codec_options, **safe_extra_kwargs)
        except SpotifyRateLimitDetectedError as e:
            d_print(f'Rate limit detected for {track_id}')
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
            # Restore original indent level if it was adjusted
            if details_indent_adjustment != 0:
                self.set_indent_number(indent_level)
            return return_with_blank_line("RATE_LIMITED")
        except Exception as e:
            # Check for Apple Music errors that should be retried
            error_str = str(e)
            if (self.service_name.lower() == 'applemusic' and
                (('failureType":"5002"' in error_str or '"failureType": "5002"' in error_str) or
                 ('status code 404' in error_str and 'Resource Not Found' in error_str))):
                if 'status code 404' in error_str:
                    d_print(f'Apple Music error: Track not found (404)')
                    # Return specific error message for Apple Music 404 (track unavailable)
                    symbols = self._get_status_symbols()
                    d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                    # Restore original indent level if it was adjusted
                    if details_indent_adjustment != 0:
                        self.set_indent_number(indent_level)
                    return return_with_blank_line("This song is unavailable.")
                else:
                    d_print(f'Apple Music temporary error (5002)')
                    symbols = self._get_status_symbols()
                    d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                    # Restore original indent level if it was adjusted
                    if details_indent_adjustment != 0:
                        self.set_indent_number(indent_level)
                    return return_with_blank_line("RATE_LIMITED")  # Reuse the rate limit retry mechanism
            # Check for rate limit in error message as a fallback
            elif "Rate limit suspected" in error_str:
                d_print(f'Rate limit detected for {track_id} (message-based detection)')
                symbols = self._get_status_symbols()
                d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                # Restore original indent level if it was adjusted
                if details_indent_adjustment != 0:
                    self.set_indent_number(indent_level)
                return return_with_blank_line("RATE_LIMITED")
            # If it's a TypeError, try the fallback approach
            if isinstance(e, TypeError):
                # Fallback for modules with simpler signatures
                # Most get_track_download methods only accept track_id and quality_tier
                try:
                    download_info: TrackDownloadInfo = self.service.get_track_download(track_id, quality_tier)
                except SpotifyRateLimitDetectedError as fallback_e:
                    d_print(f'Rate limit detected for {track_id}')
                    symbols = self._get_status_symbols()
                    d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                    # Restore original indent level if it was adjusted
                    if details_indent_adjustment != 0:
                        self.set_indent_number(indent_level)
                    return return_with_blank_line("RATE_LIMITED")
                except Exception as fallback_e:
                    # Check if this is a rate limit error even in the fallback
                    if isinstance(fallback_e, SpotifyRateLimitDetectedError):
                        d_print(f'Rate limit detected for {track_id}')
                        symbols = self._get_status_symbols()
                        d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                        # Restore original indent level if it was adjusted
                        if details_indent_adjustment != 0:
                            self.set_indent_number(indent_level)
                        return return_with_blank_line("RATE_LIMITED")
                    # Check for Apple Music errors that should be retried
                    fallback_error_str = str(fallback_e)
                    if (self.service_name.lower() == 'applemusic' and
                        (('failureType":"5002"' in fallback_error_str or '"failureType": "5002"' in fallback_error_str) or
                         ('status code 404' in fallback_error_str and 'Resource Not Found' in fallback_error_str))):
                        if 'status code 404' in fallback_error_str:
                            d_print(f'Apple Music error: Track not found (404)')
                            # Return specific error message for Apple Music 404 (track unavailable)
                            symbols = self._get_status_symbols()
                            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                            # Restore original indent level if it was adjusted
                            if details_indent_adjustment != 0:
                                self.set_indent_number(indent_level)
                            return return_with_blank_line("This song is unavailable.")
                        else:
                            d_print(f'Apple Music temporary error (5002)')
                            symbols = self._get_status_symbols()
                            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                            # Restore original indent level if it was adjusted
                            if details_indent_adjustment != 0:
                                self.set_indent_number(indent_level)
                            return return_with_blank_line("RATE_LIMITED")  # Reuse the rate limit retry mechanism
                    # Also check for rate limit in error message as a fallback
                    elif "Rate limit suspected" in fallback_error_str:
                        d_print(f'Rate limit detected for {track_id} (message-based detection)')
                        symbols = self._get_status_symbols()
                        d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                        # Restore original indent level if it was adjusted
                        if details_indent_adjustment != 0:
                            self.set_indent_number(indent_level)
                        return return_with_blank_line("RATE_LIMITED")
                    # Extract a concise error message
                    error_msg = str(fallback_e)
                    if 'status code 404' in error_msg:
                        d_print(f'Track not found (404)')
                        # Return specific error message for 404 (track unavailable)
                        symbols = self._get_status_symbols()
                        d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                        # Restore original indent level if it was adjusted
                        if details_indent_adjustment != 0:
                            self.set_indent_number(indent_level)
                        return return_with_blank_line("This song is unavailable.")
                    elif 'status code' in error_msg:
                        # Extract just the status code
                        import re
                        status_match = re.search(r'status code (\d+)', error_msg)
                        if status_match:
                            d_print(f'Request failed (status {status_match.group(1)})')
                        else:
                            d_print(f'Request failed')
                    else:
                        simplified_error = simplify_error_message(error_msg)
                        d_print(f'Download failed: {simplified_error}')
                    symbols = self._get_status_symbols()
                    d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                    # Restore original indent level if it was adjusted
                    if details_indent_adjustment != 0:
                        self.set_indent_number(indent_level)
                    return return_with_blank_line(None)
            else:
                # For non-TypeError exceptions, extract concise error message
                error_msg = str(e)
                if 'status code 404' in error_msg:
                    d_print(f'Track not found (404)')
                    # Return specific error message for 404 (track unavailable)
                    symbols = self._get_status_symbols()
                    d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                    # Restore original indent level if it was adjusted
                    if details_indent_adjustment != 0:
                        self.set_indent_number(indent_level)
                    return return_with_blank_line("This song is unavailable.")
                elif 'status code' in error_msg:
                    # Extract just the status code
                    import re
                    status_match = re.search(r'status code (\d+)', error_msg)
                    if status_match:
                        d_print(f'Request failed (status {status_match.group(1)})')
                    else:
                        d_print(f'Request failed')
                else:
                    simplified_error = simplify_error_message(error_msg)
                    d_print(f'Download failed: {simplified_error}')
                symbols = self._get_status_symbols()
                d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
                # Restore original indent level if it was adjusted
                if details_indent_adjustment != 0:
                    self.set_indent_number(indent_level)
                return return_with_blank_line(None)
        if not download_info:
            d_print(f'No download info available')
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
            # Restore original indent level if it was adjusted
            if details_indent_adjustment != 0:
                self.set_indent_number(indent_level)
            return return_with_blank_line(None)
        
        d_print('Downloading audio...')
        try:
            final_location = download_file(
                download_info.file_url,
                track_location,
                headers=download_info.file_url_headers,
                enable_progress_bar=self.global_settings['general'].get('progress_bar', False) and verbose,
                indent_level=self.indent_number
            ) if download_info.download_type is DownloadEnum.URL else shutil.move(download_info.temp_file_path, track_location)
            
            
        except Exception as download_e:
            d_print(f'Download failed with exception: {download_e}')
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
            # Restore original indent level if it was adjusted
            if details_indent_adjustment != 0:
                self.set_indent_number(indent_level)
            return return_with_blank_line(None)

        if not final_location:
            d_print(f'Failed to download track {track_id}')
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)
            # Restore original indent level if it was adjusted
            if details_indent_adjustment != 0:
                self.set_indent_number(indent_level)
            return return_with_blank_line(None)

        # Validate file size to catch corrupted downloads (fixed 100KB threshold)
        try:
            file_size = os.path.getsize(final_location)
            min_file_size = 100 * 1024  # 100KB threshold

            if file_size < min_file_size:
                d_print(f'Downloaded file is suspiciously small ({file_size:,} bytes, expected >{min_file_size:,} bytes)')
                d_print(f'File likely corrupted at source - removing incomplete download')

                # Remove the corrupted file
                try:
                    os.remove(final_location)
                except:
                    pass

                # Restore original indent level if it was adjusted before printing completion message
                if details_indent_adjustment != 0:
                    self.set_indent_number(indent_level)

                symbols = self._get_status_symbols()
                d_print(f'=== {symbols["error"]} Track failed (corrupted source) ===', drop_level=header_drop_level)
                return return_with_blank_line(None)

        except OSError as e:
            d_print(f'Could not check file size: {e}')
            # Continue with download process even if size check fails

        # Download artwork only if needed (for embedding or external saving)
        artwork_path = ''
        needs_artwork = (self.global_settings['covers']['embed_cover'] or 
                        self.global_settings['covers']['save_external'])
        
        if track_info.cover_url and needs_artwork:
            d_print('Downloading artwork...')
            artwork_path = self.create_temp_filename()
            download_file(track_info.cover_url, artwork_path, artwork_settings=self._get_artwork_settings(), indent_level=self.indent_number)

        # Do conversion BEFORE tagging (like old version)
        conversion_result = self._convert_file_if_needed(final_location, track_info, d_print)
        converted_location, old_track_location, old_container = conversion_result
        if converted_location and converted_location != final_location:
            final_location = converted_location

        # Tag file (after conversion so artwork gets embedded in converted file)
        d_print('Tagging file...')

        try:
            # Determine container from actual file extension (after potential conversion)
            file_extension = os.path.splitext(final_location)[1].lower()
            container_map = {
                '.flac': ContainerEnum.flac,
                '.mp3': ContainerEnum.mp3,
                '.m4a': ContainerEnum.m4a,
                '.opus': ContainerEnum.opus,
                '.ogg': ContainerEnum.ogg,
                '.wav': ContainerEnum.wav,
                '.aiff': ContainerEnum.aiff,
                '.ac4': ContainerEnum.ac4,
                '.ac3': ContainerEnum.ac3,
                '.eac3': ContainerEnum.eac3
            }
            container = container_map.get(file_extension, ContainerEnum.flac)
            
            
            # Get embedded lyrics if available
            embedded_lyrics = getattr(track_info, 'lyrics', None) or ''
            
            # Get credits list (empty for now)
            credits_list = []
            
            # Check if container supports tagging
            tagging_supported_containers = [ContainerEnum.flac, ContainerEnum.mp3, ContainerEnum.m4a, ContainerEnum.ogg]
            
            if container in tagging_supported_containers:
                # Tag the converted file - only pass artwork_path if embed_cover is enabled
                embed_artwork_path = artwork_path if self.global_settings['covers']['embed_cover'] else None
                tag_file(final_location, embed_artwork_path, track_info, credits_list, embedded_lyrics, container)
            else:
                pass  # Skip tagging for unsupported containers like WAV
            
            # Also tag the original file if it was kept (matching old version exactly)
            if old_track_location and old_container:
                if old_container in tagging_supported_containers:
                    embed_artwork_path = artwork_path if self.global_settings['covers']['embed_cover'] else None
                    tag_file(old_track_location, embed_artwork_path, track_info, credits_list, embedded_lyrics, old_container)
                else:
                    pass  # Skip tagging for unsupported containers
            
            if m3u_playlist:
                self._add_track_m3u_playlist(m3u_playlist, track_info, final_location)

            # Restore original indent level if it was adjusted before printing completion message
            if details_indent_adjustment != 0:
                self.set_indent_number(indent_level)

            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["success"]} Track completed ===', drop_level=header_drop_level)

            # Clean up temporary artwork file
            if artwork_path and os.path.exists(artwork_path):
                try:
                    os.remove(artwork_path)
                except OSError:
                    pass  # Ignore cleanup errors

            return return_with_blank_line(final_location)
        except Exception as e:
            # If tagging fails, treat it as a failed download for concurrent download tracking
            d_print(f'Tagging failed: {e}')
            
            # Restore original indent level if it was adjusted before printing completion message
            if details_indent_adjustment != 0:
                self.set_indent_number(indent_level)
            
            symbols = self._get_status_symbols()
            d_print(f'=== {symbols["error"]} Track failed ===', drop_level=header_drop_level)

            # Clean up temporary artwork file even on failure
            if artwork_path and os.path.exists(artwork_path):
                try:
                    os.remove(artwork_path)
                except OSError:
                    pass  # Ignore cleanup errors

            return return_with_blank_line(None)  # Return None to indicate failure for concurrent download tracking

    def _convert_file_if_needed(self, file_path, track_info, d_print):
        """Convert file based on codec_conversions settings - based on old working version"""
        try:
            # Get conversion settings (matching old version structure)
            try:
                from utils.models import CodecEnum, codec_data
                from utils.utils import silentremove
                conversions = {CodecEnum[k.upper()]: CodecEnum[v.upper()] for k, v in self.global_settings['advanced']['codec_conversions'].items()}
            except:
                conversions = {}
                print('Warning: codec_conversions setting is invalid!')  # Always print this warning
                return (file_path, None, None)  # Return tuple like old version
            
            if not conversions:
                return (file_path, None, None)  # Return tuple like old version
            
            
            # Use track_info.codec (which is already a CodecEnum) to check for conversions
            codec = track_info.codec
            
            if codec not in conversions:
                return (file_path, None, None)  # Return tuple like old version
            
            new_codec = conversions[codec]
            if codec == new_codec:
                return (file_path, None, None)  # No conversion needed
            
            # Get codec data for old and new codecs
            old_codec_data = codec_data[codec]
            new_codec_data = codec_data[new_codec]
            
            # Always print conversion status, even when verbose=False
            print(f'        Converting {old_codec_data.pretty_name} to {new_codec_data.pretty_name}...')
            
            # Check for spatial formats (skip conversion)
            if old_codec_data.spatial or new_codec_data.spatial:
                print('        Warning: converting spatial formats is not allowed, skipping')
                return (file_path, None, None)
            
            # Check for undesirable conversions (fixed logic but matching old version behavior)
            enable_undesirable = self.global_settings.get('advanced', {}).get('enable_undesirable_conversions', False)
            if not old_codec_data.lossless and new_codec_data.lossless and not enable_undesirable:
                print('        Warning: Undesirable lossy-to-lossless conversion detected, skipping')
                return (file_path, None, None)
            # Note: lossy-to-lossy conversions are allowed by default (old version had a bug that made this always allowed)
            
            # Warn about undesirable conversions but continue (matching old version)
            if not old_codec_data.lossless and new_codec_data.lossless:
                print('        Warning: Undesirable lossy-to-lossless conversion')
            elif not old_codec_data.lossless and not new_codec_data.lossless:
                print('        Warning: Undesirable lossy-to-lossy conversion')
            
            # Get conversion flags
            try:
                conversion_flags = {CodecEnum[k.upper()]:v for k,v in self.global_settings['advanced']['conversion_flags'].items()}
            except:
                conversion_flags = {}
                print('        Warning: conversion_flags setting is invalid, using defaults')
            
            conv_flags = conversion_flags[new_codec] if new_codec in conversion_flags else {}
            
            # Create temp file and final output path (matching old version exactly)
            temp_track_location = f'{self.create_temp_filename()}.{new_codec_data.container.name}'
            file_path_without_ext = os.path.splitext(file_path)[0]
            new_track_location = f'{file_path_without_ext}.{new_codec_data.container.name}'
            

            
            # Build FFmpeg stream (matching old version exactly)
            import ffmpeg
            from ffmpeg import Error
            import re
            import shutil
            
            # Get custom ffmpeg path from settings
            ffmpeg_path = self.global_settings.get('advanced', {}).get('ffmpeg_path', 'ffmpeg')
            if not ffmpeg_path or ffmpeg_path.strip() == '':
                ffmpeg_path = 'ffmpeg'
            
            import platform
            import subprocess as sp
            
            # Helper to test if an ffmpeg path works
            def test_ffmpeg(path):
                try:
                    result = sp.run([path, '-version'], capture_output=True, timeout=3)
                    return result.returncode == 0
                except:
                    return False
            
            # Try the configured path first, then fallbacks on macOS
            original_ffmpeg_path = ffmpeg_path
            if platform.system() == 'Darwin':
                if not test_ffmpeg(ffmpeg_path):
                    # Try common macOS ffmpeg locations as fallback
                    fallback_paths = [
                        '/usr/local/bin/ffmpeg',      # Homebrew Intel
                        '/opt/homebrew/bin/ffmpeg',   # Homebrew Apple Silicon
                        'ffmpeg'                       # System PATH
                    ]
                    for fallback in fallback_paths:
                        if fallback != ffmpeg_path and test_ffmpeg(fallback):
                            print(f'        [FFmpeg] Using fallback: {fallback}')
                            ffmpeg_path = fallback
                            break
            
            # Debug: print the ffmpeg path being used
            if self.global_settings.get('advanced', {}).get('debug_mode', False):
                print(f'        [Debug] Using FFmpeg path: {ffmpeg_path}')
            
            stream = ffmpeg.input(file_path, hide_banner=None, y=None)
            
            try:
                # Map codec names to FFmpeg codec names
                ffmpeg_codec_map = {
                    'wav': 'pcm_s16le',  # WAV needs PCM codec
                    'flac': 'flac',
                    'mp3': 'mp3',
                    'aac': 'aac',
                    'vorbis': 'vorbis',
                    'alac': 'alac',
                    'opus': 'opus'
                }
                ffmpeg_codec = ffmpeg_codec_map.get(new_codec.name.lower(), new_codec.name.lower())
                
                # Use the old version's approach: audio codec + ignore video streams
                stream.output(
                    temp_track_location,
                    acodec=ffmpeg_codec,
                    vn=None,  # Ignore video stream (this is key!)
                    **conv_flags,
                    loglevel='error'
                ).run(cmd=ffmpeg_path, capture_stdout=True, capture_stderr=True)
            except Error as e:
                error_msg = e.stderr.decode('utf-8')
                # Handle experimental encoder fallback (from old version)
                encoder = re.search(r"(?<=non experimental encoder ')[^']+", error_msg)
                if encoder:
                    try:
                        stream.output(
                            temp_track_location,
                            acodec=encoder.group(0),
                            vn=None,  # Ignore video stream here as well
                            **conv_flags,
                            loglevel='error'
                        ).run(cmd=ffmpeg_path, capture_stdout=True, capture_stderr=True)
                    except Error as e2:
                        raise Exception(f'ffmpeg error converting to {ffmpeg_codec}:\n{e2.stderr.decode("utf-8")}')
                else:
                    raise Exception(f'ffmpeg error converting to {ffmpeg_codec}:\n{error_msg}')
            
            # Handle file management (matching old version exactly)
            keep_original = self.global_settings.get('advanced', {}).get('conversion_keep_original', False)
            old_track_location, old_container = None, None
            
            # Remove original if output path is the same (matching old version logic)
            if file_path == new_track_location:
                silentremove(file_path)
                # just needed so it won't get deleted
                file_path = temp_track_location
            
            # Move temp file to final location
            shutil.move(temp_track_location, new_track_location)
            silentremove(temp_track_location)
            
            # Handle keeping original (matching old version exactly)
            if keep_original:
                old_track_location = file_path
                old_container = codec_data[codec].container  # Original container
            else:
                silentremove(file_path)
            
            print(f'        ✅ Conversion completed: {new_track_location}')
            
            # Return tuple: (new_location, old_location_if_kept, old_container_if_kept)
            return (new_track_location, old_track_location, old_container)
            
        except Exception as e:
            # Check if it's an FFmpeg-related error and provide user-friendly message
            error_str = str(e)
            if any(indicator in error_str.lower() for indicator in [
                'winerror 2', 'errno 2', 'no such file or directory', 
                'het systeem kan het opgegeven bestand niet vinden',
                'file not found', 'ffmpeg', 'executable not found',
                'operation not permitted', 'permission denied'
            ]):
                print(f'        ❌ Conversion error: FFmpeg was not found or is misconfigured. This is required for audio conversion.')
                print(f'        💡 Solution: Install FFmpeg or set the path in Settings > Global > Advanced > FFmpeg Path')
                import platform
                if platform.system() == 'Darwin':
                    print(f'        💡 macOS: Install via Homebrew: brew install ffmpeg')
                elif platform.system() == 'Linux':
                    print(f'        💡 Linux: Install via package manager:')
                    print(f'           Ubuntu/Debian: sudo apt install ffmpeg')
                    print(f'           Fedora: sudo dnf install ffmpeg')
                    print(f'           Arch: sudo pacman -S ffmpeg')
            else:
                print(f'        ❌ Conversion error: {e}')
            return (file_path, None, None)  # Return tuple like old version

    def _get_artwork_settings(self, module_name = None, is_external = False):
        if not module_name:
            module_name = self.service_name
        return {
            'should_resize': ModuleFlags.needs_cover_resize in self.module_settings[module_name].flags,
            'resolution': self.global_settings['covers']['external_resolution'] if is_external else self.global_settings['covers']['main_resolution'],
            'compression': self.global_settings['covers']['external_compression'] if is_external else self.global_settings['covers']['main_compression'],
            'format': self.global_settings['covers']['external_format'] if is_external else 'jpg'
        }
