#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

'''
Telecharge automatiquement certaines emissions AlloCiné.

Le code a bien évolué avec le temps parce que le site a changé,
certaines opérations ne sont peut etre plus nécessaires.

Pour télécharger une nouvelle émission, il suffit d'ajouter l'url de sa page principale.

'''

import sys, os, glob, re, json, html, requests
import time, datetime
import urllib.parse
import email.utils
import youtube_dl
try:
    import http.client as http_client
except ImportError:
    import httplib as http_client

# TODO
# sanitize filepath
# more checks
# betters regexp
# log
# resume download
# rsync mode

USER_AGENT = 'Mozilla/5.0 (X11; Linux i686; rv:95.0) Gecko/20100101 Firefox/95.0'

class Allocine():
    
    def __init__(self, dryrun=False, debug=False):
        self.root_folder = os.path.dirname(os.path.realpath(__file__))
        self.downloader = Downloader(dryrun)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})
        self.shows = []
        if debug:
            http_client.HTTPConnection.debuglevel = 1

    def add_show(self, title, url, folder=None):
        if not folder:
            folder = os.path.join(self.root_folder, '%%TITLE%%', '%%TITLE%% - Saison %%SEASON%%')
        self.shows.append(Show(title, url, folder))

    def clean_episode_title(self, episode_title):
        episode_title = html.unescape(episode_title)
        episode_title = episode_title.replace('"', '')
        episode_title = episode_title.replace('/', '&')
        episode_title = episode_title.replace('\xc2\xb0', '')
        episode_title = episode_title.replace('Les gaffes de ', '')
        episode_title = episode_title.replace('Les gaffes du ', 'Le ')
        episode_title = episode_title.replace('Les gaffes et erreurs de ', '')
        episode_title = episode_title.replace('Les gaffes et erreurs d\'', '')
        episode_title = episode_title.replace('Les gaffes et erreurs des ', 'Les ')
        episode_title = episode_title.replace(' ...', '...')
        episode_title = episode_title.replace(' ,', ',')
        episode_title = episode_title.replace('« ', '')
        episode_title = episode_title.replace(' »', '')
        episode_title = episode_title.replace('N°', 'N')
        episode_title = episode_title.replace('Merci Qui?', 'Merci Qui ?')
        episode_title = episode_title.replace('Merci qui - ', '')
        episode_title = re.sub(' +', ' ', episode_title)
        episode_title = episode_title.strip()
        return episode_title
    
    def format_episode_title(self, show, episode_title):
        episode_title = self.clean_episode_title(episode_title)

        if not episode_title.startswith(show.title) and show.title != 'Direct 2 DVD': # TODO: WTF?
            episode_title = '%s - %s' % (show.title, episode_title)
    
        # generic
        u = re.search(r'(.*) N(\d*) - (.*)', episode_title)
        if u and len(u.groups()) == 3:
            return '%s - s%02de%03d - %s' % (u.group(1), season['nb'], int(u.group(2)), u.group(3))

        # Dedans AlloCiné season 1 :
        u = re.search(r'(.*) - S(\d*)E(\d*) : (.*)', episode_title)
        if u and len(u.groups()) == 4:
            return '%s - s%02de%02d - %s' % (u.group(1), season['nb'], int(u.group(3)), u.group(4))

        # Dedans AlloCiné season 2 :
        u = re.search(r'(.*) S(\d*) E(\d*) - (.*)', episode_title)
        if u and len(u.groups()) == 4:
            return '%s - s%02de%02d - %s' % (u.group(1), season['nb'], int(u.group(3)), u.group(4))

        return episode_title
    
    def get_episodes_urls(self, url):
        urls = []
        page = 1
        while True:
            r = self.session.get('%s/?page=%d' % (url, page)) # page is now useless?
            if r.status_code != 200:
                break
            u = re.findall(r'<a class="meta-title-link" href="(.*)">', r.text)
            if not u:
                break
            if u[0] in urls:
                break
            urls += u
            page += 1
        return ['https://www.allocine.fr%s' % s for s in urls]
    
    def extract_media_id(self, url, data):
        u = re.findall(r'/video-(\d.*)/', url)
        if u:
            return u[0]
        u = re.findall(r'player\.allocine\.fr/(.+?)\.html",', data)
        if u:
            return u[0]

    def get_mp4_url(self, episode_media_id):
        definitions = ['hdPath', 'mdPath', 'ldPath']
        
        r = self.session.get('https://www.allocine.fr/ws/AcVisiondataV5.ashx?media=%s' % episode_media_id)
        try:
            video = json.loads(r.text).get('video')
            for definition in definitions:
                url = video.get(definition)
                r = self.session.head(url)
                if r.status_code == 200:
                    return url
        except ValueError as e:
            pass

        # no MP4 URL, video may be stored by Arte (TODO: better code to retrieve URL)
        r = self.session.get('https://www.allocine.fr/_video/iblogvision.aspx?cmedia=%s' % episode_media_id)
        u = re.findall(r'api\.arte\.tv/api/player/v1/config/fr/(.*)\?', urllib.parse.unquote(r.text))
        if not u:
            return None
        
        r = self.session.get('https://api.arte.tv/api/player/v1/config/fr/%s?platform=CREATIVE&config=arte_creative' % u[0])
        try:
            videoJsonPlayer = json.loads(r.text).get('videoJsonPlayer')
            if 'VSR' in videoJsonPlayer and videoJsonPlayer.get('VSR'):
                url = videoJsonPlayer.get('VSR').get('HTTPS_SQ_1').get('url')
                r = self.session.head(url)
                if r.status_code == 200:
                    return url
            if 'customMsg' in videoJsonPlayer and videoJsonPlayer.get('customMsg'): 
                msg = videoJsonPlayer.get('customMsg').get('msg')
                print(">>>> %s" % msg) # TODO
        except ValueError as e:
            pass

        return None
        
    def download_season(self, show, season):

        folder = show.folder
        folder = folder.replace('%%TITLE%%', show.title)
        folder = folder.replace('%%SEASON%%', '%02d' % season['nb'])
        
        urls = self.get_episodes_urls(season['url'])
        if not urls:
            print(' Invalid URL (%s): no episode found.' % season['url'])
            return None

        if not os.path.isdir(folder):
            os.makedirs(folder, 0o755)

        failed = []
        print('[-] This season has %d episodes...' % len(urls))
        for count, url in enumerate(urls):
            r = self.session.get(url)
            
            # TODO: now we can have all information (episode title, video URL) from https://www.allocine.fr/ws/AcVisiondataV5.ashx?media=<episode_media_id>

            # extract episode title
            u = re.search(r'<meta property="og:title" content="(.*)" />', r.text)
            if not u:
                print(' Unable to extract episode title from %s' % url)
                failed += ['%s - episode title not found' % url]
                continue
            
            episode_title = self.format_episode_title(show, u.group(1))

            print('[%03d/%03d] %s (%s)' % (count + 1, len(urls), episode_title, url))
            
            # do not try to download if file is already on disk
            if os.path.exists(os.path.join(folder, '%s.mp4' % episode_title)):
                print(' Already downloaded!')
                return None # return, don't continue (assume first url is the last episode)
            
            episode_media_id = self.extract_media_id(url, r.text)
            if not episode_media_id:
                print(' Unable to extract episode media ID from %s' % url)
                failed += ['%s - episode media id not found' % url]
                continue

            mp4_url = self.get_mp4_url(episode_media_id)
            if not mp4_url:
                print(' Unable to get video URL...')
                failed += ['%s - %s' % (url, episode_title)]
                continue

            if mp4_url.startswith('youtube:'):
                if not self.downloader.download_with_youtubedl(mp4_url[8:], folder, '%s.mp4' % episode_title):
                    print(' Unable to download %s with youtube_dl' % mp4_url)
                    failed += ['%s - %s - %s' % (url, episode_title, mp4_url)]
            else:
                if not self.downloader.download_with_progessbar(mp4_url, folder, '%s.mp4' % episode_title):
                    print(' Unable to download %s' % mp4_url)
                    failed += ['%s - %s - %s' % (url, episode_title, mp4_url)]

        return failed


class Show():

    def __init__(self, title, url, folder):
        self.title = title
        self.url = url
        self.folder = folder
        self.current_season_local = 0
        self.seasons = []
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})

    def get_seasons(self):
        # last season number from local disk
        pattern = self.folder.replace('%%TITLE%%', self.title)
        pattern = pattern.replace('%%SEASON%%', '*')
        for folder in glob.glob(pattern):
            if not os.listdir(folder):
                continue
            n = int(re.findall('Saison (\d.*)', folder)[0]) # TODO: this regexp only works with default folder pattern
            self.current_season_local = max(self.current_season_local, n)
            
        # all seasons URL from AlloCiné
        r = self.session.get(self.url)
        if r.status_code != 200:
            return False
        
        path    = urllib.parse.urlparse(self.url).path
        pattern = r'href="/%s/saison-(\d.*)/" title="(.*)"' % path.strip('/')
        for m in re.findall(pattern, r.text):
            n = int(re.findall('Saison (\d.*)', m[1])[0])
            if n >= self.current_season_local: # only keep not yet downloaded seasons
                self.seasons.append({'nb': n, 'url': '%ssaison-%d' % (self.url, int(m[0]))})

        if not self.seasons:
            return False

        self.seasons = sorted(self.seasons, key=lambda d: d['nb']) 
        
        return True


class Downloader():
    
    def __init__(self, dryrun):
        self.dryrun = dryrun
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})

    def download_with_progessbar(self, url, folder, file_name, retry=3):
        print(' Downloading from %s' % url)

        with self.session.get(url, stream=True) as r:
            file_size = int(r.headers.get('content-length'))
            file_ts   = time.mktime(email.utils.parsedate(r.headers.get('last-modified')))
            file_path = os.path.join(folder, file_name) # todo: sanitize filepath
            
            if os.path.exists(file_path) and os.path.getsize(file_path) == file_size:
                print(' Already downloaded!')
                return True
            
            if self.dryrun:
                print(' Dry-run mode, not really downloading...')
                return True
            
            with open(file_path, 'wb') as f:
                dl = 0
                for data in r.iter_content(chunk_size=8192):
                    dl += len(data)
                    f.write(data)
                    status = '%10d/%d  [%3.2f%%]' % (dl, file_size, dl * 100. / file_size)
                    status = status + chr(8) * (len(status) + 1)
                    print(status, end=' ')
                print('')
            
            if os.path.getsize(file_path) != file_size:
                print(' Error during download! (file size is %d, should be %d) retry=%d' % (os.path.getsize(file_path), file_size, retry))
                os.unlink(file_path)
                if retry:
                    return self.download_with_progessbar(url, folder, file_name, retry - 1)
                return False
            os.utime(file_path, (file_ts, file_ts))
        return True

    def download_with_youtubedl(self, id, folder, file_name):
        print(' Downloading from YouTube:%s' % id)
        
        youtube_dl.utils.std_headers['User-Agent'] = USER_AGENT

        file_path = os.path.join(folder, file_name) # todo: sanitize filepath
        
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            print(' Already downloaded!')
            return True

        if self.dryrun:
            print(' Dry-run mode, not really downloading...')
            return True

        ydl_opts = {
            'format': 'best',
            'outtmpl': file_path,
            'noplaylist' : True,
            'continue_dl': True,
        }
        try:
            with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                ydl.download(['https://www.youtube.com/watch?v=%s' % id])
        except youtube_dl.utils.DownloadError as e:
            print(e)
            return False

        return True


if __name__ == '__main__':

    allocine = Allocine(dryrun=False, debug=False)
    #allocine.add_show('Nanaroscope', 'https://www.allocine.fr/video/programme-21543/')
    #allocine.add_show('Dedans AlloCiné', 'https://www.allocine.fr/video/programme-11027/')
    #allocine.add_show('Escale à Nanarland', 'https://www.allocine.fr/video/programme-12285/')
    #allocine.add_show('Merci Qui ?', 'https://www.allocine.fr/video/programme-12294/')
    #allocine.add_show('Direct to DVD', 'https://www.allocine.fr/video/programme-12287/') # Direct to DVD -> Home Cinema -> La Minute Reco
    #allocine.add_show('La Minute Reco', 'https://www.allocine.fr/video/programme-27577/')

    allocine.add_show('Et paf, il est mort', 'https://www.allocine.fr/video/programme-25113/')
    allocine.add_show('Faux Raccord', 'https://www.allocine.fr/video/programme-12284/')
    allocine.add_show('Clichés', 'https://www.allocine.fr/video/programme-24834/')
    allocine.add_show('Give Me Five', 'https://www.allocine.fr/video/programme-21919/')
    allocine.add_show('The Big Fan Theory', 'https://www.allocine.fr/video/programme-20403/')
    allocine.add_show('Aviez-vous remarqué ?', 'https://www.allocine.fr/video/programme-19518/')
    allocine.add_show('Top 5', 'https://www.allocine.fr/video/programme-12299/')
    allocine.add_show('Fanzone', 'https://www.allocine.fr/video/programme-12298/')
    allocine.add_show('Origin Story', 'https://www.allocine.fr/video/programme-25667/')
    

    for show in allocine.shows:
        if not show.get_seasons():
            print(f'[!] {show.title} - Unable to get seasons information.')
            continue
        for season in show.seasons:
            print(f'[+] {show.title} - Downloading season #{season["nb"]}...')
            failed = allocine.download_season(show, season)
            if failed:
                print('Failed:')
                for f in failed:
                    print(' - %s' % f)
            print(f'[-] {show.title} - Season #{season["nb"]} done.')

    sys.exit(0)
