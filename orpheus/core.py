import importlib, json, logging, os, pickle, requests, urllib3, base64, shutil, sys
from datetime import datetime

from orpheus.music_downloader import Downloader
from utils.models import *
from utils.utils import *
from utils.exceptions import *

os.environ['CURL_CA_BUNDLE'] = ''  # Hack to disable SSL errors for requests module for easier debugging
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # Make SSL warnings hidden

# try:
#     time_request = requests.get('https://github.com') # to be replaced with something useful, like an Orpheus updates json
# except:
#     print('Could not reach the internet, quitting')
#     exit()

# timestamp_correction_term = int(datetime.strptime(time_request.headers['Date'], '%a, %d %b %Y %H:%M:%S GMT').timestamp() - datetime.utcnow().timestamp())
# if abs(timestamp_correction_term) > 60*60*24:
#     print('System time is incorrect, using online time to correct it for subscription expiry checks')

timestamp_correction_term = 0
# Use the same Oprinter instance wherever it's needed
oprinter = Oprinter()


def true_current_utc_timestamp():
    return int(datetime.utcnow().timestamp()) + timestamp_correction_term


class Orpheus:
    def __init__(self, private_mode=False):
        self.extensions, self.extension_list, self.module_list, self.module_settings, self.module_netloc_constants, self.loaded_modules = {}, set(), set(), {}, {}, {}
        self.gui_handlers = {}

        self.default_global_settings = {
            "general": {
                "download_path": "./downloads/",
                "download_quality": "hifi",
                "search_limit": 25,
                "concurrent_downloads": 5,
                "progress_bar": False
            },
            "artist_downloading":{
                "return_credited_albums": True,
                "separate_tracks_skip_downloaded": True
            },
            "formatting": {
                "album_format": "{artist}/{name}",
                "playlist_format": "{name}",
                "track_filename_format": "{artist} - {name}",
                "single_full_path_format": "{artist} - {name}",
                "enable_zfill": True,
                "force_album_format": False
            },
            "codecs": {
                "proprietary_codecs": False,
                "spatial_codecs": True
            },
            "module_defaults": {
                "lyrics": "default",
                "covers": "default",
                "credits": "default"
            },
            "lyrics": {
                "embed_lyrics": True,
                "embed_synced_lyrics": False,
                "save_synced_lyrics": True
            },
            "covers": {
                "embed_cover": True,
                "main_compression": "high",
                "main_resolution": 1400,
                "save_external": False,
                "external_format": 'png',
                "external_compression": "low",
                "external_resolution": 3000,
                "save_animated_cover": True
            },
            "playlist": {
                "save_m3u": True,
                "paths_m3u": "absolute",
                "extended_m3u": True
            },
            "advanced": {
                "advanced_login_system": False,
                "codec_conversions": {
                    "alac": "flac",
                    "wav": "flac",
                    "vorbis": "vorbis"
                },
                "conversion_flags": {
                    "flac": {
                        "compression_level": "5"
                    },
                    "mp3": {
                        "qscale:a": "0"
                    },
                    "aac": {
                        "audio_bitrate": "256k"
                    }
                },
                "conversion_keep_original": False,
                "ffmpeg_path": "ffmpeg",
                "cover_variance_threshold": 8,
                "debug_mode": False,
                "disable_subscription_checks": False,
                "enable_undesirable_conversions": False,
                "ignore_existing_files": False,
                "ignore_different_artists": True
            }
        }

        self.data_folder_base = 'config'
        self.settings_location = os.path.join(self.data_folder_base, 'settings.json')
        self.session_storage_location = os.path.join(self.data_folder_base, 'loginstorage.bin')

        os.makedirs('config', exist_ok=True)
        self.settings = json.loads(open(self.settings_location, 'r').read()) if os.path.exists(self.settings_location) else {}

        try:
            if self.settings['global']['advanced']['debug_mode']: 
                logging.basicConfig(level=logging.DEBUG)
            else:
                # Configure logging to suppress Spotify module warnings/errors
                logging.basicConfig(level=logging.CRITICAL)
                # Specifically suppress common Spotify authentication messages
                logging.getLogger('modules.spotify.spotify_api').setLevel(logging.CRITICAL)
                logging.getLogger('librespot').setLevel(logging.CRITICAL)
                logging.getLogger('spotify').setLevel(logging.CRITICAL)
        except KeyError:
            # Configure logging to suppress Spotify module warnings/errors even if no settings
            logging.basicConfig(level=logging.CRITICAL)
            logging.getLogger('modules.spotify.spotify_api').setLevel(logging.CRITICAL)
            logging.getLogger('librespot').setLevel(logging.CRITICAL)
            logging.getLogger('spotify').setLevel(logging.CRITICAL)

        os.makedirs('extensions', exist_ok=True)
        for extension in os.listdir('extensions'):  # Loading extensions
            if os.path.isdir(f'extensions/{extension}') and os.path.exists(f'extensions/{extension}/interface.py'):
                class_ = getattr(importlib.import_module(f'extensions.{extension}.interface'), 'OrpheusExtension', None)
                if class_:
                    self.extension_list.add(extension)
                    logging.debug(f'Orpheus: {extension} extension detected')
                else:
                    raise Exception('Error loading extension: "{extension}"')

        # Module preparation (not loaded yet for performance purposes)
        # Modules in this set are skipped during discovery (e.g. deprecated/removed but folder may remain on macOS upgrades)
        modules_ignored = {'jiosaavn'}
        os.makedirs('modules', exist_ok=True)
        module_list = [m.lower() for m in os.listdir('modules') if m.lower() not in modules_ignored and os.path.exists(f'modules/{m}/interface.py')]
        if not module_list or module_list == ['example']:
            raise Exception('No modules are installed. Please install at least one module in the modules folder.')
        logging.debug('Orpheus: Modules detected: ' + ", ".join(module_list))

        for module in module_list:  # Loading module information into module_settings
            module_information: ModuleInformation = getattr(importlib.import_module(f'modules.{module}.interface'), 'module_information', None)
            if module_information and not ModuleFlags.private in module_information.flags and not private_mode:
                self.module_list.add(module)
                self.module_settings[module] = module_information
                logging.debug(f'Orpheus: {module} added as a module')
            else:
                raise Exception(f'Error loading module information from module: "{module}"') # TODO: replace with InvalidModuleError

        duplicates = set()
        for module in self.module_list: # Detecting duplicate url constants
            module_info: ModuleInformation = self.module_settings[module]
            url_constants = module_info.netlocation_constant
            if not isinstance(url_constants, list): url_constants = [str(url_constants)]
            for constant in url_constants:
                if constant.startswith('setting.'):
                    if self.settings.get('modules') and self.settings['modules'].get(module):
                        constant = self.settings['modules'][module][constant.split('setting.')[1]]
                    else:
                        constant = None

                if constant:
                    if constant not in self.module_netloc_constants:
                        self.module_netloc_constants[constant] = module
                    elif ModuleFlags.private in module_info.flags: # Replacing public modules with private ones
                        if ModuleFlags.private in self.module_settings[constant].flags: duplicates.add(constant)
                    else:
                        duplicates.add(tuple(sorted([module, self.module_netloc_constants[constant]])))
        if duplicates:
            duplicate_msgs = []
            for d in duplicates:
                if isinstance(d, (list, tuple)):
                    duplicate_msgs.append(' and '.join(d))
                else:
                    duplicate_msgs.append(str(d))
            raise Exception('Multiple modules installed that connect to the same service names: ' + ', '.join(duplicate_msgs))

        self.update_module_storage()

        for i in self.extension_list:
            extension_settings: ExtensionInformation = getattr(importlib.import_module(f'extensions.{i}.interface'), 'extension_settings', None)
            settings = self.settings['extensions'][extension_settings.extension_type][extension] \
                if extension_settings.extension_type in self.settings['extensions'] \
                and extension in self.settings['extensions'][extension_settings.extension_type] else extension_settings.settings
            extension_type = extension_settings.extension_type
            self.extensions[extension_type] = self.extensions[extension_type] if extension_type in self.extensions else {}
            self.extensions[extension_type][extension] = class_(settings)

        [self.load_module(module) for module in self.module_list if ModuleFlags.startup_load in self.module_settings[module].flags]

        self.module_controls = {'module_list': self.module_list, 'module_settings': self.module_settings,
            'loaded_modules': self.loaded_modules, 'module_loader': self.load_module}

    def register_gui_handler(self, handler_name: str, handler_func):
        """Registers a GUI handler function for core/module interaction."""
        self.gui_handlers[handler_name] = handler_func
        logging.info(f"Registered GUI handler: {handler_name}")

    def load_module(self, module: str):
        module = module.lower()
        if module not in self.module_list:
            raise Exception(f'"{module}" does not exist in modules.') # TODO: replace with InvalidModuleError
        if module not in self.loaded_modules:
            class_ = getattr(importlib.import_module(f'modules.{module}.interface'), 'ModuleInterface', None)
            if class_:
                class ModuleError(Exception): # TODO: get rid of this, as it is deprecated
                    def __init__(self, message):
                        super().__init__(module + ' --> ' + str(message))

                # Get settings with fallbacks to defaults for robustness on first run
                global_settings = self.settings.get('global', {})
                general_settings = global_settings.get('general', self.default_global_settings.get('general', {}))
                advanced_settings = global_settings.get('advanced', self.default_global_settings.get('advanced', {}))
                covers_settings = global_settings.get('covers', self.default_global_settings.get('covers', {}))
                
                module_controller = ModuleController(
                    module_settings = self.settings['modules'][module] if module in self.settings.get('modules', {}) else {},
                    data_folder = os.path.join(self.data_folder_base, 'modules', module),
                    extensions = self.extensions,
                    temporary_settings_controller = TemporarySettingsController(module, self.session_storage_location),
                    module_error = ModuleError, # DEPRECATED
                    get_current_timestamp = true_current_utc_timestamp,
                    printer_controller = oprinter,
                    orpheus_options = OrpheusOptions(
                        debug_mode = advanced_settings.get('debug_mode', False),
                        quality_tier = QualityEnum[general_settings.get('download_quality', 'hifi').upper()],
                        disable_subscription_check = advanced_settings.get('disable_subscription_checks', False),
                        default_cover_options = CoverOptions(
                            file_type = ImageFileTypeEnum[covers_settings.get('external_format', 'png')],
                            resolution = covers_settings.get('main_resolution', 1400),
                            compression = CoverCompressionEnum[covers_settings.get('main_compression', 'high')]
                        )
                    ),
                    gui_handlers = self.gui_handlers,
                    progress_bar_enabled = general_settings.get('progress_bar', True)
                )

                loaded_module = class_(module_controller)
                self.loaded_modules[module] = loaded_module

                # Check if module has settings
                settings = self.settings.get('modules', {}).get(module, {})
                temporary_session = read_temporary_setting(self.session_storage_location, module)
                if self.module_settings[module].login_behaviour is ManualEnum.orpheus:
                    # Login if simple mode (email or username + password) when requested by update_setting_storage
                    if temporary_session and temporary_session['clear_session'] and not advanced_settings.get('advanced_login_system', False):
                        hashes = {k: hash_string(str(v)) for k, v in settings.items()}
                        if not temporary_session.get('hashes') or \
                            any(k not in hashes or hashes[k] != v for k,v in temporary_session['hashes'].items() if k in self.module_settings[module].session_settings):
                            username_or_email = (settings.get('email') or settings.get('username') or '').strip()
                            password = (settings.get('password') or '').strip()
                            if username_or_email and password:
                                print('Logging into ' + self.module_settings[module].service_name)
                                try:
                                    loaded_module.login(settings['email'] if 'email' in settings else settings['username'], settings['password'])
                                except:
                                    set_temporary_setting(self.session_storage_location, module, 'hashes', None, {})
                                    raise
                                set_temporary_setting(self.session_storage_location, module, 'hashes', None, hashes)
                    if ModuleFlags.enable_jwt_system in self.module_settings[module].flags and temporary_session and \
                            temporary_session['refresh'] and not temporary_session['bearer']:
                        loaded_module.refresh_login()

                data_folder = os.path.join(self.data_folder_base, 'modules', module)
                if ModuleFlags.uses_data in self.module_settings[module].flags and not os.path.exists(data_folder): os.makedirs(data_folder)

                logging.debug(f'Orpheus: {module} module has been loaded')
                return loaded_module
            else:
                raise Exception(f'Error loading module: "{module}"') # TODO: replace with InvalidModuleError
        else:
            return self.loaded_modules[module]

    def update_module_storage(self): # Should be refactored eventually
        ## Settings
        old_settings, new_settings, global_settings, extension_settings, module_settings, new_setting_detected = {}, {}, {}, {}, {}, False

        for i in ['global', 'extensions', 'modules']:
            old_settings[i] = self.settings[i] if i in self.settings else {}

        for setting_type in self.default_global_settings:
            if setting_type in old_settings['global']:
                global_settings[setting_type] = {}
                for setting in self.default_global_settings[setting_type]:
                    # Also check if the type is identical
                    if (setting in old_settings['global'][setting_type] and
                            isinstance(self.default_global_settings[setting_type][setting],
                                       type(old_settings['global'][setting_type][setting]))):
                        global_settings[setting_type][setting] = old_settings['global'][setting_type][setting]
                    else:
                        global_settings[setting_type][setting] = self.default_global_settings[setting_type][setting]
                        new_setting_detected = True
            else:
                global_settings[setting_type] = self.default_global_settings[setting_type]
                new_setting_detected = True

        for i in self.extension_list:
            extension_information: ExtensionInformation = getattr(importlib.import_module(f'extensions.{i}.interface'), 'extension_settings', None)
            extension_type = extension_information.extension_type
            extension_settings[extension_type] = {} if 'extension_type' not in extension_information else extension_information[extension_type]
            old_settings['extensions'][extension_type] = {} if extension_type not in old_settings['extensions'] else old_settings['extensions'][extension_type]
            extension_settings[extension_type][i] = {} # This code regenerates the settings
            for j in extension_information.settings:
                if i in old_settings['extensions'][extension_type] and j in old_settings['extensions'][extension_type][i]:
                    extension_settings[extension_type][i][j] = old_settings['extensions'][extension_type][i][j]
                else:
                    extension_settings[extension_type][i][j] = extension_information.settings[j]
                    new_setting_detected = True

        advanced_login_mode = global_settings['advanced']['advanced_login_system']
        for i in self.module_list:
            module_settings[i] = {} # This code regenerates the settings
            if advanced_login_mode:
                settings_to_parse = self.module_settings[i].global_settings
            else:
                settings_to_parse = {**self.module_settings[i].global_settings, **self.module_settings[i].session_settings}
            if settings_to_parse:
                for j in settings_to_parse:
                    if i in old_settings['modules'] and j in old_settings['modules'][i]:
                        module_settings[i][j] = old_settings['modules'][i][j]
                    else:
                        module_settings[i][j] = settings_to_parse[j]
                        new_setting_detected = True
            else:
                module_settings.pop(i)

        new_settings['global'] = global_settings
        new_settings['extensions'] = extension_settings
        new_settings['modules'] = module_settings

        ## Sessions
        sessions = pickle.load(open(self.session_storage_location, 'rb')) if os.path.exists(self.session_storage_location) else {}

        if not ('advancedmode' in sessions and 'modules' in sessions and sessions['advancedmode'] == advanced_login_mode):
            sessions = {'advancedmode': advanced_login_mode, 'modules':{}}

        # in format {advancedmode, modules: {modulename: {default, type, custom_data, sessions: [sessionname: {##}]}}}
        # where ## is 'custom_session' plus if jwt 'access, refresh' (+ emailhash in simple)
        # in the special case of simple mode, session is always called default
        new_module_sessions = {}
        for i in self.module_list:
            # Clear storage if type changed
            new_module_sessions[i] = sessions['modules'][i] if i in sessions['modules'] else {'selected':'default', 'sessions':{'default':{}}}

            if self.module_settings[i].global_storage_variables: new_module_sessions[i]['custom_data'] = \
                {j:new_module_sessions[i]['custom_data'][j] for j in self.module_settings[i].global_storage_variables \
                    if 'custom_data' in new_module_sessions[i] and j in new_module_sessions[i]['custom_data']}

            # Migration/Fix for list-based sessions (legacy or corrupted)
            if isinstance(new_module_sessions[i]['sessions'], list):
                 first_session = new_module_sessions[i]['sessions'][0] if new_module_sessions[i]['sessions'] else {}
                 new_module_sessions[i]['sessions'] = {'default': first_session}
                 new_module_sessions[i]['selected'] = 'default'

            for current_session in new_module_sessions[i]['sessions'].values():
                # For simple login type only, as it does not apply to advanced login
                if self.module_settings[i].login_behaviour is ManualEnum.orpheus and not advanced_login_mode:
                    hashes = {k:hash_string(str(v)) for k,v in module_settings[i].items()}
                    if current_session.get('hashes'):
                        clear_session = any(k not in hashes or hashes[k] != v for k,v in current_session['hashes'].items() if k in self.module_settings[i].session_settings)
                    else:
                        clear_session = True
                else:
                    clear_session = False
                current_session['clear_session'] = clear_session

                if ModuleFlags.enable_jwt_system in self.module_settings[i].flags:
                    if 'bearer' in current_session and current_session['bearer'] and not clear_session:
                        # Clears bearer token if it's expired
                        try:
                            time_left_until_refresh = json.loads(base64.b64decode(current_session['bearer'].split('.')[0]))['exp'] - true_current_utc_timestamp()
                            current_session['bearer'] = current_session['bearer'] if time_left_until_refresh > 0 else ''
                        except:
                            pass
                    else:
                        current_session['bearer'] = ''
                        current_session['refresh'] = ''
                else:
                    if 'bearer' in current_session: current_session.pop('bearer')
                    if 'refresh' in current_session: current_session.pop('refresh')

                if self.module_settings[i].session_storage_variables: current_session['custom_data'] = \
                    {j:current_session['custom_data'][j] for j in self.module_settings[i].session_storage_variables \
                        if 'custom_data' in current_session and j in current_session['custom_data'] and not clear_session}
                elif 'custom_data' in current_session: current_session.pop('custom_data')

        pickle.dump({'advancedmode': advanced_login_mode, 'modules': new_module_sessions}, open(self.session_storage_location, 'wb'))
        open(self.settings_location, 'w').write(json.dumps(new_settings, indent = 4, sort_keys = False))

        if new_setting_detected:
            print('New settings detected, or the configuration has been reset. Please update settings.json')
            # Don't exit in GUI mode - just print the message and continue
            # The GUI will handle showing appropriate messages to the user

    def get_merged_global_settings(self):
        """Returns global settings merged with defaults to ensure all keys exist."""
        merged = {}
        current_global = self.settings.get('global', {})
        for section_name, section_defaults in self.default_global_settings.items():
            if isinstance(section_defaults, dict):
                merged[section_name] = {**section_defaults, **current_global.get(section_name, {})}
            else:
                merged[section_name] = current_global.get(section_name, section_defaults)
        return merged


def orpheus_core_download(orpheus_session: Orpheus, media_to_download, third_party_modules, separate_download_module, output_path, use_ansi_colors=True):
    # Get global settings merged with defaults to ensure all required keys exist
    global_settings = orpheus_session.get_merged_global_settings()
    downloader = Downloader(global_settings, orpheus_session.module_controls, oprinter, output_path, use_ansi_colors)
    downloader.full_settings = orpheus_session.settings  # Add access to full settings including modules
    os.makedirs('temp', exist_ok=True)

    for mainmodule, items in media_to_download.items():
        total_items_in_batch = len(items)
        
        for index, media in enumerate(items, start=1):
            if ModuleModes.download not in orpheus_session.module_settings[mainmodule].module_supported_modes:
                raise Exception(f'{mainmodule} does not support track downloading') # TODO: replace with ModuleDoesNotSupportAbility

            # Load and prepare module
            music = orpheus_session.load_module(mainmodule)
            downloader.service = music
            downloader.service_name = mainmodule

            for i in third_party_modules:
                moduleselected = third_party_modules[i]
                if moduleselected:
                    if moduleselected not in orpheus_session.module_list:
                        raise Exception(f'{moduleselected} does not exist in modules.') # TODO: replace with InvalidModuleError
                    elif i not in orpheus_session.module_settings[moduleselected].module_supported_modes:
                        raise Exception(f'Module {moduleselected} does not support {i}') # TODO: replace with ModuleDoesNotSupportAbility
                    else:
                        # If all checks pass, load up the selected module
                        orpheus_session.load_module(moduleselected)

            downloader.third_party_modules = third_party_modules

            mediatype = media.media_type
            media_id = media.media_id

            downloader.download_mode = mediatype

            # Mode to download playlist using other service
            if separate_download_module != 'default' and separate_download_module != mainmodule:
                if mediatype is not DownloadTypeEnum.playlist:
                    raise Exception('The separate download module option is only for playlists.') # TODO: replace with ModuleDoesNotSupportAbility
                downloader.download_playlist(media_id, custom_module=separate_download_module, extra_kwargs=media.extra_kwargs)
            else:  # Standard download modes
                if mediatype is DownloadTypeEnum.album:
                    downloader.download_album(media_id, extra_kwargs=media.extra_kwargs)
                elif mediatype is DownloadTypeEnum.track:
                    downloader.set_indent_number(1)  # Set proper indentation for track downloads
                    
                    # For single track downloads, show Pass 1 only for Spotify (which has retry passes)
                    pass_indicator = f" (Pass 1)" if (total_items_in_batch > 1 and mainmodule.lower() == 'spotify') else ""
                    if total_items_in_batch > 1:
                        # Track headers should have 8 spaces indentation (don't drop the indent level)
                        downloader.print(f'Track {index}/{total_items_in_batch}{pass_indicator}')
                    
                    download_result = downloader.download_track(
                        media_id, 
                        number_of_tracks=total_items_in_batch,
                        extra_kwargs=media.extra_kwargs,
                        indent_level=1
                    )
                    
                    # Add rate limiting for individual track downloads (like from urls.txt)
                    # Only pause if track was actually downloaded (not skipped) and not the last track
                    if (mainmodule.lower() == 'spotify' and index < total_items_in_batch and 
                        download_result is not None and download_result != "RATE_LIMITED"):
                        pause_seconds = downloader._get_spotify_pause_seconds()
                        # Don't add extra blank line - track completion already handles spacing
                        downloader.print(f'Pausing {pause_seconds} seconds to prevent rate limiting...', drop_level=1)
                        import time
                        time.sleep(pause_seconds)
                    
                    # Collect rate-limited tracks for retry (only for Spotify and multiple tracks)
                    if (download_result == "RATE_LIMITED" and mainmodule.lower() == 'spotify' and 
                        total_items_in_batch > 1):
                        # Store rate-limited track info for later retry
                        if not hasattr(downloader, 'rate_limited_tracks'):
                            downloader.rate_limited_tracks = []
                        downloader.rate_limited_tracks.append({
                            'media': media,
                            'original_index': index
                        })
                elif mediatype is DownloadTypeEnum.playlist:
                    downloader.download_playlist(media_id, extra_kwargs=media.extra_kwargs)
                elif mediatype is DownloadTypeEnum.artist:
                    downloader.download_artist(media_id, extra_kwargs=media.extra_kwargs)
                elif mediatype is DownloadTypeEnum.label:
                    downloader.download_label(media_id, extra_kwargs=media.extra_kwargs)
                else:
                    raise Exception(f'\tUnknown media type "{mediatype}"')

        # Handle retry for rate-limited individual tracks (only for Spotify and multiple tracks)
        if mainmodule.lower() == 'spotify' and total_items_in_batch > 1:
            # Add blank line after all tracks are processed
            print()
            
            if hasattr(downloader, 'rate_limited_tracks') and downloader.rate_limited_tracks:
                num_deferred = len(downloader.rate_limited_tracks)
                downloader.print(f'{num_deferred} tracks deferred due to rate limiting. Retrying...', drop_level=0)
                
                for i, retry_info in enumerate(downloader.rate_limited_tracks):
                    media = retry_info['media']
                    original_index = retry_info['original_index']
                    
                    downloader.print(f'Track {original_index}/{total_items_in_batch} (Retry Pass)', drop_level=1)
                    
                    download_result = downloader.download_track(
                        media.media_id,
                        number_of_tracks=total_items_in_batch,
                        extra_kwargs=media.extra_kwargs,
                        indent_level=1
                    )
                    
                    # Add 30-second pause between retry tracks (except for the last one)
                    if i < len(downloader.rate_limited_tracks) - 1:
                        print()  # Add blank line before pause message
                        downloader.print('Pausing 30 seconds to prevent rate limiting...', drop_level=1)
                        import time
                        time.sleep(30)
                
                # Clear the rate-limited tracks list after retry
                downloader.rate_limited_tracks = []
            else:
                # Show "no tracks deferred" message only for multiple track downloads
                downloader.print('No tracks were deferred due to rate limiting.', drop_level=0)
                print()  # Add blank line after message

    if os.path.exists('temp'): shutil.rmtree('temp')