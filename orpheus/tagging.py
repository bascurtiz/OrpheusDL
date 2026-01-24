import base64
import logging
import os
import tempfile
from dataclasses import asdict

from PIL import Image
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType, APIC, USLT, TDAT, COMM, TPUB
from mutagen.mp3 import EasyMP3
from mutagen.mp4 import MP4Cover
from mutagen.mp4 import MP4Tags
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.oggvorbis import OggVorbisHeaderError
import mutagen

from utils.exceptions import *
from utils.models import ContainerEnum, TrackInfo

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


def tag_file(file_path: str, image_path: str, track_info: TrackInfo, credits_list: list, embedded_lyrics: str, container: ContainerEnum):
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

        # Register encoded, rating, barcode, compatible_brands, major_brand and minor_version
        tagger.tags.RegisterTextKey('encoded', 'TSSE')
        tagger.tags.RegisterTXXXKey('compatible_brands', 'compatible_brands')
        tagger.tags.RegisterTXXXKey('major_brand', 'major_brand')
        tagger.tags.RegisterTXXXKey('minor_version', 'minor_version')
        tagger.tags.RegisterTXXXKey('Rating', 'Rating')
        tagger.tags.RegisterTXXXKey('upc', 'BARCODE')

        tagger.tags.pop('encoded', None)
    elif container == ContainerEnum.m4a:
        tagger = EasyMP4(file_path)

        # Register ISRC, lyrics, cover and explicit tags
        tagger.RegisterTextKey('isrc', '----:com.apple.itunes:ISRC')
        tagger.RegisterTextKey('upc', '----:com.apple.itunes:UPC')
        tagger.RegisterTextKey('explicit', 'rtng') if track_info.explicit is not None else None
        tagger.RegisterTextKey('covr', 'covr')
        tagger.RegisterTextKey('lyrics', '\xa9lyr') if embedded_lyrics else None
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

    tagger['title'] = track_info.name
    if track_info.album: tagger['album'] = track_info.album
    if track_info.tags.album_artist: tagger['albumartist'] = track_info.tags.album_artist

    tagger['artist'] = track_info.artists

    if container == ContainerEnum.m4a or container == ContainerEnum.mp3:
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

    if track_info.tags.release_date:
        if container == ContainerEnum.mp3:
            # Never access protected attributes, too bad! Only works on ID3v2.4, disabled for now!
            # tagger.tags._EasyID3__id3._DictProxy__dict['TDRL'] = TDRL(encoding=3, text=track_info.tags.release_date)
            # Use YYYY-MM-DD for consistency and convert it to DDMM
            release_dd_mm = f'{track_info.tags.release_date[8:10]}{track_info.tags.release_date[5:7]}'
            tagger.tags._EasyID3__id3._DictProxy__dict['TDAT'] = TDAT(encoding=3, text=release_dd_mm)
            # Now add the year tag
            tagger['date'] = str(track_info.release_year)
        else:
            tagger['date'] = track_info.tags.release_date
    else:
        tagger['date'] = str(track_info.release_year)

    if track_info.tags.copyright:tagger['copyright'] = track_info.tags.copyright

    if track_info.explicit is not None:
        if container == ContainerEnum.m4a:
            tagger['explicit'] = b'\x01' if track_info.explicit else b'\x02'
        elif container == ContainerEnum.mp3:
            tagger['Rating'] = 'Explicit' if track_info.explicit else 'Clean'
        else:
            tagger['Rating'] = 'Explicit' if track_info.explicit else 'Clean'

    if track_info.tags.genres: tagger['genre'] = track_info.tags.genres
    if track_info.tags.isrc: tagger['isrc'] = track_info.tags.isrc.encode() if container == ContainerEnum.m4a else track_info.tags.isrc
    if track_info.tags.upc: tagger['UPC'] = track_info.tags.upc.encode() if container == ContainerEnum.m4a else track_info.tags.upc

    # add the label tag
    if track_info.tags.label:
        if container in {ContainerEnum.flac, ContainerEnum.ogg, ContainerEnum.webm}:
            tagger['Label'] = track_info.tags.label
        elif container == ContainerEnum.mp3:
            tagger.tags._EasyID3__id3._DictProxy__dict['TPUB'] = TPUB(
                encoding=3,
                text=track_info.tags.label
            )
        elif container == ContainerEnum.m4a:
            # only works with MP3TAG? https://docs.mp3tag.de/mapping/
            tagger.RegisterTextKey('label', '\xa9pub')
            tagger['label'] = track_info.tags.label

    # add the description tag
    if track_info.tags.description and container == ContainerEnum.m4a:
        tagger.RegisterTextKey('desc', 'description')
        tagger['description'] = track_info.tags.description

    # add comment tag
    if track_info.tags.comment:
        if container == ContainerEnum.m4a:
            tagger.RegisterTextKey('comment', '\xa9cmt')
            tagger['comment'] = track_info.tags.comment
        elif container == ContainerEnum.mp3:
            tagger.tags._EasyID3__id3._DictProxy__dict['COMM'] = COMM(
                encoding=3,
                lang=u'eng',
                desc=u'',
                text=track_info.tags.description
            )

    # add all extra_kwargs key value pairs to the (FLAC, Vorbis) file
    if container in {ContainerEnum.flac, ContainerEnum.ogg, ContainerEnum.webm}:
        for key, value in track_info.tags.extra_tags.items():
            tagger[key] = value
    elif container is ContainerEnum.m4a:
        for key, value in track_info.tags.extra_tags.items():
            # Create a new freeform atom and set the extra_tags in bytes
            tagger.RegisterTextKey(key, '----:com.apple.itunes:' + key)
            tagger[key] = str(value).encode()

    # Need to change to merge duplicate credits automatically, or switch to plain dicts instead of list[dataclass]
    if credits_list:
        if container == ContainerEnum.m4a:
            for credit in credits_list:
                # Create a new freeform atom and set the contributors in bytes
                tagger.RegisterTextKey(credit.type, '----:com.apple.itunes:' + credit.type)
                tagger[credit.type] = [con.encode() for con in credit.names]
        elif container == ContainerEnum.mp3:
            for credit in credits_list:
                # Create a new user-defined text frame key
                tagger.tags.RegisterTXXXKey(credit.type.upper(), credit.type)
                tagger[credit.type] = credit.names
        else:
            for credit in credits_list:
                try:
                    tagger.tags[credit.type] = credit.names
                except:
                    pass

    if embedded_lyrics:
        if container == ContainerEnum.mp3:
            # Never access protected attributes, too bad! I hope I never have to write ID3 code again
            tagger.tags._EasyID3__id3._DictProxy__dict['USLT'] = USLT(
                encoding=3,
                lang=u'eng',  # don't assume?
                text=embedded_lyrics
            )
        else:
            tagger['lyrics'] = embedded_lyrics

    if track_info.tags.replay_gain and track_info.tags.replay_peak and container != ContainerEnum.m4a:
        tagger['REPLAYGAIN_TRACK_GAIN'] = str(track_info.tags.replay_gain)
        tagger['REPLAYGAIN_TRACK_PEAK'] = str(track_info.tags.replay_peak)

    # Handle cover art embedding/removal
    if image_path:
        # Always clear existing cover art first to prevent duplicates (especially for Beatport/Beatsource)
        if container == ContainerEnum.flac:
            tagger.clear_pictures()
        elif container == ContainerEnum.m4a:
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
                elif container == ContainerEnum.m4a:
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
        elif container == ContainerEnum.m4a:
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
        tagger.save(file_path, v1=2, v2_version=3, v23_sep=None) if container == ContainerEnum.mp3 else tagger.save()
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
