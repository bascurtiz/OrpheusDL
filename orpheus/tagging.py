import base64
import logging
import os
import tempfile
from dataclasses import asdict

from PIL import Image
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType, APIC, USLT, TDAT, COMM, TPUB, TCON
from mutagen.mp3 import EasyMP3
from mutagen.mp4 import MP4Cover
from mutagen.mp4 import MP4Tags
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.oggvorbis import OggVorbisHeaderError
import mutagen

from utils.exceptions import *
from utils.models import ContainerEnum, TrackInfo
from utils.utils import get_primary_artist

# Needed for Windows tagging support
MP4Tags._padding = 0


def _resize_image_if_needed(image_path: str, max_size_bytes: int = 16 * 1024 * 1024, target_resolution: tuple = (3000, 3000)) -> str:
    """
    Resize an image if it exceeds the maximum file size.
    
    Args:
        image_path: Path to the original image
        max_size_bytes: Maximum allowed file size in bytes (default: 16MB)
        target_resolution: Target resolution as (width, height) tuple (default: 3000x3000)
    
    Returns:
        Path to the resized image (temporary file) or original path if no resize needed
    """
    # Check if the original file size is within limits
    if os.path.getsize(image_path) <= max_size_bytes:
        return image_path
    
    try:
        # Open and resize the image
        with Image.open(image_path) as img:
            # Convert to RGB if necessary (handles RGBA, P, etc.)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Resize maintaining aspect ratio, fitting within target_resolution
            img.thumbnail(target_resolution, Image.Resampling.LANCZOS)
            
            # Create a temporary file for the resized image
            temp_fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix='orpheus_resized_')
            os.close(temp_fd)  # Close the file descriptor, we'll use the path
            
            # Save the resized image with high quality
            img.save(temp_path, 'JPEG', quality=90, optimize=True)
            

            
            return temp_path
            
    except Exception as e:
        print(f'\tFailed to resize cover image: {e}. Using original image.')
        return image_path


def tag_file(file_path: str, image_path: str, track_info: TrackInfo, credits_list: list, embedded_lyrics: str, container: ContainerEnum, metadata_separator: str = ';', split_metadata: bool = True):
    if container == ContainerEnum.flac:
        tagger = FLAC(file_path)
    elif container == ContainerEnum.opus:
        tagger = OggOpus(file_path)
    elif container == ContainerEnum.ogg:
        tagger = OggVorbis(file_path)
    elif container == ContainerEnum.mp3:
        tagger = EasyMP3(file_path)

        if tagger.tags is None:
            tagger.tags = EasyID3()  # Add EasyID3 tags if none are present

        # Register standard and fallback keys for EasyID3
        tagger.tags.RegisterTextKey('encoded', 'TSSE')
        tagger.tags.RegisterTextKey('publisher', 'TPUB')
        tagger.tags.RegisterTXXXKey('label', 'LABEL')
        tagger.tags.RegisterTXXXKey('publisher_txxx', 'PUBLISHER')
        tagger.tags.RegisterTXXXKey('recordlabel', 'RECORDLABEL')
        tagger.tags.RegisterTXXXKey('upc', 'BARCODE')
        tagger.tags.RegisterTXXXKey('barcode', 'BARCODE')
        tagger.tags.RegisterTXXXKey('genre_txxx', 'GENRE')
        tagger.tags.RegisterTXXXKey('compatible_brands', 'compatible_brands')
        tagger.tags.RegisterTXXXKey('major_brand', 'major_brand')
        tagger.tags.RegisterTXXXKey('minor_version', 'minor_version')
        tagger.tags.RegisterTXXXKey('Rating', 'Rating')
        tagger.tags.RegisterTXXXKey('track_url', 'TRACK_URL')

        tagger.tags.pop('encoded', None)
    elif container == ContainerEnum.m4a or container == ContainerEnum.mp4:
        tagger = MP4(file_path)
    elif container == ContainerEnum.webm:
        tagger = mutagen.File(file_path)
        if tagger is None:
            # If mutagen fails to identify it, we can't tag it easily without matroska support
            raise Exception('Mutagen could not identify WebM file for tagging. Consider converting to Opus/Ogg.')
    else:
        raise Exception('Unknown container for tagging')

    # Remove all useless MPEG-DASH ffmpeg tags
    if tagger.tags is not None:
        if 'major_brand' in tagger.tags:
            del tagger.tags['major_brand']
        if 'minor_version' in tagger.tags:
            del tagger.tags['minor_version']
        if 'compatible_brands' in tagger.tags:
            del tagger.tags['compatible_brands']
        if 'encoder' in tagger.tags:
            del tagger.tags['encoder']

    if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
        # Raw MP4 atom names for standard tags
        tagger['\xa9nam'] = [track_info.name]
        if track_info.album: tagger['\xa9alb'] = [track_info.album]
        if track_info.tags.album_artist:
            album_artist_display = get_primary_artist(track_info.tags.album_artist)
            tagger['aART'] = [album_artist_display]
        if split_metadata:
            tagger['\xa9ART'] = track_info.artists if isinstance(track_info.artists, list) else [track_info.artists]
        else:
            tagger['\xa9ART'] = [metadata_separator.join(track_info.artists) if isinstance(track_info.artists, list) else track_info.artists]
    else:
        tagger['title'] = track_info.name
        if track_info.album: tagger['album'] = track_info.album
        # Album artist
        if track_info.tags.album_artist:
            album_artist_display = get_primary_artist(track_info.tags.album_artist)
            
            if container in {ContainerEnum.flac, ContainerEnum.ogg, ContainerEnum.opus, ContainerEnum.webm}:
                tagger['ALBUMARTIST'] = album_artist_display
            else:
                tagger['albumartist'] = album_artist_display

        if split_metadata:
            tagger['artist'] = track_info.artists if isinstance(track_info.artists, list) else [track_info.artists]
        else:
            tagger['artist'] = metadata_separator.join(track_info.artists) if isinstance(track_info.artists, list) else track_info.artists

    if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
        # MP4 uses tuple format: [(track_number, total_tracks)]
        tn = track_info.tags.track_number or 0
        tt = track_info.tags.total_tracks or 0
        if tn or tt:
            tagger['trkn'] = [(tn, tt)]
        dn = track_info.tags.disc_number or 0
        dt = track_info.tags.total_discs or 0
        if dn or dt:
            tagger['disk'] = [(dn, dt)]
    elif container == ContainerEnum.mp3:
        if track_info.tags.track_number and track_info.tags.total_tracks:
            tagger['tracknumber'] = str(track_info.tags.track_number) + '/' + str(track_info.tags.total_tracks)
        elif track_info.tags.track_number:
            tagger['tracknumber'] = str(track_info.tags.track_number)
        if track_info.tags.disc_number and track_info.tags.total_discs:
            tagger['discnumber'] = str(track_info.tags.disc_number) + '/' + str(track_info.tags.total_discs)
        elif track_info.tags.disc_number:
            tagger['discnumber'] = str(track_info.tags.disc_number)
    else:
        if track_info.tags.track_number: tagger['tracknumber'] = str(track_info.tags.track_number)
        if track_info.tags.disc_number: tagger['discnumber'] = str(track_info.tags.disc_number)
        if track_info.tags.total_tracks: tagger['totaltracks'] = str(track_info.tags.total_tracks)
        if track_info.tags.total_discs: tagger['totaldiscs'] = str(track_info.tags.total_discs)

    if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
        if track_info.tags.release_date:
            tagger['\xa9day'] = [track_info.tags.release_date]
        else:
            tagger['\xa9day'] = [str(track_info.release_year)]
        if track_info.tags.copyright:
            tagger['cprt'] = [track_info.tags.copyright]
        if track_info.tags.composer:
            tagger['\xa9wrt'] = [metadata_separator.join(track_info.tags.composer) if isinstance(track_info.tags.composer, list) else track_info.tags.composer]
    else:
        if track_info.tags.release_date:
            if container == ContainerEnum.mp3:
                release_dd_mm = f'{track_info.tags.release_date[8:10]}{track_info.tags.release_date[5:7]}'
                tagger.tags._EasyID3__id3._DictProxy__dict['TDAT'] = TDAT(encoding=3, text=release_dd_mm)
                tagger['date'] = str(track_info.release_year)
            else:
                tagger['date'] = track_info.tags.release_date
        else:
            tagger['date'] = str(track_info.release_year)
        if track_info.tags.copyright: tagger['copyright'] = track_info.tags.copyright
        if track_info.tags.composer:
            if container == ContainerEnum.mp3:
                tagger['composer'] = metadata_separator.join(track_info.tags.composer) if isinstance(track_info.tags.composer, list) else track_info.tags.composer
            else:
                tagger['COMPOSER'] = track_info.tags.composer

    if track_info.explicit is not None:
        if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            tagger['rtng'] = [1 if track_info.explicit else 0]
        elif container == ContainerEnum.mp3:
            tagger['Rating'] = 'Explicit' if track_info.explicit else 'Clean'
        else:
            tagger['Rating'] = 'Explicit' if track_info.explicit else 'Clean'

    if track_info.tags.genres:
        if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            if split_metadata:
                tagger['\xa9gen'] = track_info.tags.genres if isinstance(track_info.tags.genres, list) else [track_info.tags.genres]
            else:
                tagger['\xa9gen'] = [metadata_separator.join(track_info.tags.genres) if isinstance(track_info.tags.genres, list) else track_info.tags.genres]
        else:
            if split_metadata:
                tagger['genre'] = track_info.tags.genres if isinstance(track_info.tags.genres, list) else [track_info.tags.genres]
            else:
                tagger['genre'] = metadata_separator.join(track_info.tags.genres) if isinstance(track_info.tags.genres, list) else track_info.tags.genres
            
    if track_info.tags.isrc:
        if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            tagger['----:com.apple.itunes:ISRC'] = [track_info.tags.isrc.encode()]
        elif container in {ContainerEnum.ogg, ContainerEnum.flac, ContainerEnum.opus, ContainerEnum.webm}:
            tagger['ISRC'] = track_info.tags.isrc
        else:
            tagger['isrc'] = track_info.tags.isrc

    if track_info.tags.upc:
        if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            tagger['----:com.apple.itunes:BARCODE'] = [track_info.tags.upc.encode()]
        elif container in {ContainerEnum.ogg, ContainerEnum.flac, ContainerEnum.opus, ContainerEnum.webm}:
            tagger['BARCODE'] = track_info.tags.upc
            tagger['UPC'] = track_info.tags.upc
        else:
            tagger['upc'] = track_info.tags.upc
            tagger['barcode'] = track_info.tags.upc

    # add the label tag
    if track_info.tags.label:
        if container in {ContainerEnum.flac, ContainerEnum.ogg, ContainerEnum.opus, ContainerEnum.webm}:
            # ORGANIZATION only here — LABEL/PUBLISHER written after credits to prevent overwrite
            tagger['ORGANIZATION'] = track_info.tags.label
        elif container == ContainerEnum.mp3:
            # Write ORGANIZATION via EasyID3 (maps to TPUB) — rest done as raw frames at the end
            tagger['organization'] = track_info.tags.label
        elif container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            # \xa9pub = standard publisher atom (shows as PUBLISHER in Mp3tag)
            # ----:com.apple.itunes:LABEL = freeform label atom
            tagger['\xa9pub'] = [track_info.tags.label]
            tagger['----:com.apple.itunes:LABEL'] = [track_info.tags.label.encode()]

    # add the track url tag
    if track_info.tags.track_url:
        if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            tagger['----:com.apple.itunes:TRACK_URL'] = [track_info.tags.track_url.encode()]
        elif container in {ContainerEnum.ogg, ContainerEnum.flac, ContainerEnum.opus, ContainerEnum.webm}:
            tagger['TRACK_URL'] = track_info.tags.track_url
        else:
            tagger['track_url'] = track_info.tags.track_url

    # add the description tag
    if track_info.tags.description and (container == ContainerEnum.m4a or container == ContainerEnum.mp4):
        tagger['desc'] = [track_info.tags.description]

    # add comment tag
    if track_info.tags.comment:
        if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            tagger['\xa9cmt'] = [track_info.tags.comment]
        elif container == ContainerEnum.mp3:
            tagger.tags._EasyID3__id3._DictProxy__dict['COMM'] = COMM(
                encoding=3,
                lang=u'eng',
                desc=u'',
                text=track_info.tags.comment
            )

    # add all extra_kwargs key value pairs to the (FLAC, Vorbis) file
    if container in {ContainerEnum.flac, ContainerEnum.ogg, ContainerEnum.opus, ContainerEnum.webm}:
        for key, value in track_info.tags.extra_tags.items():
            tagger[key] = value
    elif container == ContainerEnum.m4a or container == ContainerEnum.mp4:
        for key, value in track_info.tags.extra_tags.items():
            tagger['----:com.apple.itunes:' + key] = [str(value).encode()]

    # Group and merge duplicate credits automatically
    if credits_list:
        grouped_credits = {}
        for credit in credits_list:
            credit_type = credit.type.lower()
            if credit_type not in grouped_credits:
                grouped_credits[credit_type] = []
            grouped_credits[credit_type].extend(credit.names)

        # Filter out redundant 'Main Artist' credits if they overlap with album or track artist
        if track_info.tags.album_artist or track_info.artists:
            # Handle album_artist being either a string or a list
            album_artist_list = track_info.tags.album_artist if isinstance(track_info.tags.album_artist, list) else [track_info.tags.album_artist] if track_info.tags.album_artist else []
            album_artist_lowers = [a.lower() for a in album_artist_list]
            
            # Use lower-case artists for comparison
            track_artist_lowers = [a.lower() for a in (track_info.artists if isinstance(track_info.artists, list) else [track_info.artists])]
            
            credits_to_remove = []
            for credit_type, names in grouped_credits.items():
                normalized_type = credit_type.replace('_', ' ').replace('-', ' ').strip().lower()
                
                # Always remove music publisher credits as requested
                if normalized_type == 'music publisher':
                    credits_to_remove.append(credit_type)
                    continue

                if normalized_type in {'main artist', 'primary artist'}:
                    credit_names_lower = [n.lower() for n in names]
                    # Redundant if it matches the album artist exactly OR the track artist list
                    if any(a in credit_names_lower for a in album_artist_lowers) or \
                       credit_names_lower == track_artist_lowers or \
                       ' & '.join(names).lower() in album_artist_lowers:
                        credits_to_remove.append(credit_type)
            
            for credit_type in credits_to_remove:
                del grouped_credits[credit_type]

        if container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            for credit_type, names in grouped_credits.items():
                if split_metadata:
                    tagger['----:com.apple.itunes:' + credit_type] = [name.encode() for name in names]
                else:
                    tagger['----:com.apple.itunes:' + credit_type] = [metadata_separator.join(names).encode()]
        elif container == ContainerEnum.mp3:
            for credit_type, names in grouped_credits.items():
                key = credit_type.upper()
                tagger.tags.RegisterTXXXKey(key, credit_type)
                try:
                    if split_metadata:
                        tagger[key] = names
                    else:
                        tagger[key] = [metadata_separator.join(names)]
                except Exception:
                    pass
        else:
            for credit_type, names in grouped_credits.items():
                try:
                    if split_metadata:
                        tagger[credit_type] = names
                    else:
                        tagger[credit_type] = [metadata_separator.join(names)]
                except Exception:
                    pass

    # Re-apply label/publisher AFTER credits to prevent credit data from overwriting them
    # (credits loop can overwrite these since VorbisComment keys are case-insensitive)
    if track_info.tags.label:
        if container in {ContainerEnum.flac, ContainerEnum.ogg, ContainerEnum.opus, ContainerEnum.webm}:
            tagger['LABEL'] = track_info.tags.label
            tagger['PUBLISHER'] = track_info.tags.label
            tagger['ORGANIZATION'] = track_info.tags.label
        elif container == ContainerEnum.mp3:
            # Write publisher directly as raw ID3 frame to bypass EasyID3 key issues
            tagger.tags._EasyID3__id3._DictProxy__dict['TPUB'] = TPUB(
                encoding=3,
                text=[track_info.tags.label]
            )


    if embedded_lyrics:
        if container == ContainerEnum.mp3:
            # Use proper add() method for USLT frame
            tagger.tags._EasyID3__id3.add(USLT(
                encoding=3,
                lang=u'eng',
                desc=u'',
                text=embedded_lyrics
            ))
        elif container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            tagger['\xa9lyr'] = [embedded_lyrics]
        else:
            tagger['lyrics'] = embedded_lyrics

    if track_info.tags.replay_gain and track_info.tags.replay_peak and container != ContainerEnum.m4a and container != ContainerEnum.mp4:
        tagger['REPLAYGAIN_TRACK_GAIN'] = str(track_info.tags.replay_gain)
        tagger['REPLAYGAIN_TRACK_PEAK'] = str(track_info.tags.replay_peak)

    # Handle cover art embedding/removal
    if image_path:
        # Always clear existing cover art first to prevent duplicates (especially for Beatport/Beatsource)
        if container == ContainerEnum.flac:
            tagger.clear_pictures()
        elif container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            if 'covr' in tagger:
                del tagger['covr']
        elif container == ContainerEnum.mp3:
            if hasattr(tagger.tags, '_EasyID3__id3') and 'APIC' in tagger.tags._EasyID3__id3:
                del tagger.tags._EasyID3__id3['APIC']
        elif container in {ContainerEnum.ogg, ContainerEnum.opus}:
            if 'metadata_block_picture' in tagger:
                del tagger['metadata_block_picture']
        
        # Embed new cover art
        # Resize image if it's too large
        resized_image_path = _resize_image_if_needed(image_path, max_size_bytes=16 * 1024 * 1024)
        temp_file_created = resized_image_path != image_path
        
        try:
            with open(resized_image_path, 'rb') as c:
                data = c.read()
            picture = Picture()
            picture.data = data

            # Check if cover is smaller than 16MB (should always be true after resizing)
            if len(picture.data) < picture._MAX_SIZE:
                if container == ContainerEnum.flac:
                    picture.type = PictureType.COVER_FRONT
                    picture.mime = u'image/jpeg'
                    tagger.add_picture(picture)
                elif container == ContainerEnum.m4a or container == ContainerEnum.mp4:
                    tagger['covr'] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
                elif container == ContainerEnum.mp3:
                    # Never access protected attributes, too bad!
                    tagger.tags._EasyID3__id3._DictProxy__dict['APIC'] = APIC(
                        encoding=3,  # UTF-8
                        mime='image/jpeg',
                        type=3,  # album art
                        desc='Cover',  # name
                        data=data
                    )
                # If you want to have a cover in only a few applications, then this technically works for Opus
                elif container in {ContainerEnum.ogg, ContainerEnum.opus}:
                    im = Image.open(resized_image_path)
                    width, height = im.size
                    picture.type = 17
                    picture.desc = u'Cover Art'
                    picture.mime = u'image/jpeg'
                    picture.width = width
                    picture.height = height
                    picture.depth = 24
                    encoded_data = base64.b64encode(picture.write())
                    tagger['metadata_block_picture'] = [encoded_data.decode('ascii')]
            else:
                print(f'\tCover file size is still too large after resizing, only {(picture._MAX_SIZE / 1024 ** 2):.2f}MB are allowed. Track '
                      f'will not have cover saved.')
        finally:
            # Clean up temporary file if one was created
            if temp_file_created and os.path.exists(resized_image_path):
                try:
                    os.unlink(resized_image_path)
                except OSError:
                    pass  # Ignore cleanup errors
    else:
        # Remove existing cover art when embed_cover is disabled
        if container == ContainerEnum.flac:
            # Remove all pictures from FLAC file
            tagger.clear_pictures()
        elif container == ContainerEnum.m4a or container == ContainerEnum.mp4:
            # Remove cover art from MP4/M4A file
            if 'covr' in tagger:
                del tagger['covr']
        elif container == ContainerEnum.mp3:
            # Remove APIC frame from MP3 file
            if hasattr(tagger.tags, '_EasyID3__id3') and 'APIC' in tagger.tags._EasyID3__id3:
                del tagger.tags._EasyID3__id3['APIC']
        elif container in {ContainerEnum.ogg, ContainerEnum.opus}:
            # Remove cover art from Ogg/Opus file
            if 'metadata_block_picture' in tagger:
                del tagger['metadata_block_picture']
        elif container == ContainerEnum.webm:
            # Matroska cover art removal (attachments)
            # This is complex in mutagen, often attachments are separate. 
            # For now, we might skip clearing if it's too complex, or just rely on overwriting.
            pass

    try:
        tagger.save(file_path, v1=2, v2_version=3, v23_sep='; ') if container == ContainerEnum.mp3 else tagger.save()
    except OggVorbisHeaderError as ogg_header_error:
        # Check if it's the specific "unable to read full header" error for Ogg Vorbis
        if "unable to read full header" in str(ogg_header_error).lower():
            logging.warning(f"Mutagen OggVorbisHeaderError ('unable to read full header') for {file_path}. Metadata might be missing or incomplete.")
            # We don't raise TagSavingFailure here to allow the process to continue, 
            # but we log it as a warning since the user reported missing metadata.
        else:
            # It's a different OggVorbisHeaderError, so proceed with the original fallback.
            logging.error(f"Tagging failed for {file_path} with OggVorbisHeaderError: {ogg_header_error}", exc_info=True)
            tag_text = '\n'.join((f'{k}: {v}' for k, v in asdict(track_info.tags).items() if v and k != 'credits' and k != 'lyrics'))
            tag_text += '\n\ncredits:\n    ' + '\n    '.join(f'{credit.type}: {", ".join(credit.names)}' for credit in credits_list if credit.names) if credits_list else ''
            tag_text += '\n\nlyrics:\n    ' + '\n    '.join(embedded_lyrics.split('\n')) if embedded_lyrics else ''
            open(file_path.rsplit('.', 1)[0] + '_tags.txt', 'w', encoding='utf-8').write(tag_text)
            raise TagSavingFailure
    except Exception as e: # Catch other general exceptions from tagger.save()
        logging.error(f"Generic tagging failed for {file_path}. Error: {e}", exc_info=True) # Log the actual error
        tag_text = '\n'.join((f'{k}: {v}' for k, v in asdict(track_info.tags).items() if v and k != 'credits' and k != 'lyrics'))
        tag_text += '\n\ncredits:\n    ' + '\n    '.join(f'{credit.type}: {", ".join(credit.names)}' for credit in credits_list if credit.names) if credits_list else ''
        tag_text += '\n\nlyrics:\n    ' + '\n    '.join(embedded_lyrics.split('\n')) if embedded_lyrics else ''
        open(file_path.rsplit('.', 1)[0] + '_tags.txt', 'w', encoding='utf-8').write(tag_text)
        raise TagSavingFailure # Re-raise TagSavingFailure for other generic errors
