#!/usr/bin/python3
# YMAudio: Yandex.Music Audio Player

import vlc, cimg, notify2, webbrowser, yandex_music
import dbus.service, dbus.mainloop.glib
from .auth import *
from gi.repository import GLib
from Scurses import *
from utils import *; logstart('YMAudio')

ym_login: str
ym_pw: str
ym_token: str

db.setfile('~/YMAudio.db')
db.setbackup(False)
db.setsensitive(True)
db.register('ym_login', 'ym_pw', 'ym_token')

yandex_music.Client.notice_displayed = True

COVER_MIN_SIZE = '30x30'

def ym_is_unauthorized(ex):
	try: return ('ownerOtherwiseUserBindingError' in ex.args[0])
	except (IndexError, KeyError): return False

class MediaPlayer2(dbus.service.Object):
	class _Properties(SlotsOnly):
		app: SCApp

		def __init__(self, app):
			self.app = app

		def to_dict(self):
			return {k: v.fget(self) if (isinstance(v, property)) else v for k, v in inspect.getmembers(self) if not k.startswith('_') and k not in ('app', 'to_dict')}

	class Properties_org_mpris_MediaPlayer2(_Properties):
		CanQuit = True
		CanRaise = False
		HasTrackList = False # TODO
		Identity = 'YMAudio'
		SupportedUriSchemes = ['']
		SupportedMimeTypes = ['']

	class Properties_org_mpris_MediaPlayer2_Player(_Properties):
		Shuffle = False
		MinimumRate = 0.1
		MaximumRate = 10.0
		CanGoNext = True
		CanGoPrevious = True
		CanPlay = True
		CanPause = True
		CanSeek = True
		CanControl = True

		@property
		def Rate(self):
			return self.app.p.get_rate()

		@Rate.setter
		def Rate(self, rate):
			self.app.p.set_rate(rate)

		@property
		def Volume(self):
			return self.app.p.audio_get_volume()/100

		@Volume.setter
		def Volume(self, volume):
			self.app.p.audio_set_volume(volume*100)

		@property
		def PlaybackStatus(self):
			return 'Playing' if (self.app.p.is_playing()) else 'Paused' if (self.app.track) else 'Stopped'

		@property
		def LoopStatus(self):
			return 'Track' if (self.app.repeat) else 'None'

		@LoopStatus.setter
		def LoopStatus(self, loop):
			self.app.repeat = (loop != 'None')

		@property
		def Metadata(self):
			track = self.app.track
			return dbus.Dictionary({
				'mpris:trackid': dbus.ObjectPath(f"/org/mpris/MediaPlayer2/ymaudio/track/{str(track.track_id).replace(':', '_').replace('-', '_')}" if (track) else '/org/mpris/MediaPlayer2/TrackList/NoTrack'),
				'mpris:length': dbus.Int64(max(0, self.app.p.get_length())*1000),
				**(S({
					'mpris:artUrl': self.app.get_cover(track.cover_uri),
					'xesam:artist': track.artists_name() or ('',),
					'xesam:title': track.title,
					'xesam:url': self.app.get_url(track),
					'xesam:asText': self.app.get_lyrics(track),
				}).filter(None) if (track) else {}),
			}, signature='sv')

		@property
		def Position(self):
			return dbus.Int64(max(0, self.app.p.get_time())*1000)

		@Position.setter
		def Position(self, position):
			self.app.p.set_time(position/1000)

	def __init__(self, app, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.app = app
		self.properties_org_mpris_MediaPlayer2 = self.Properties_org_mpris_MediaPlayer2(self.app)
		self.properties_org_mpris_MediaPlayer2_Player = self.Properties_org_mpris_MediaPlayer2_Player(self.app)

	@dbus.service.method('org.mpris.MediaPlayer2')
	def Raise(self):
		pass

	@dbus.service.method('org.mpris.MediaPlayer2')
	def Quit(self):
		self.app.quit()

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def Next(self):
		self.app.playNextTrack()

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def Previous(self):
		self.app.playPrevTrack()

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def Pause(self):
		self.app.pause()

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def PlayPause(self):
		self.app.playPause()

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def Stop(self):
		self.app.stop()

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def Play(self):
		self.app.play()

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def Seek(self, offset):
		self.properties_org_mpris_MediaPlayer2_Player.Position += offset

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def SetPosition(self, trackid, position):
		self.properties_org_mpris_MediaPlayer2_Player.Position = position

	@dbus.service.method('org.mpris.MediaPlayer2.Player')
	def OpenUri(self, uri):
		pass

	@dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
	def Get(self, interface, prop):
		return getattr(getattr(self, 'properties_'+interface.replace('.', '_')), prop)

	@dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
	def GetAll(self, interface):
		return getattr(self, 'properties_'+interface.replace('.', '_')).to_dict()

	@dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ssv')
	def Set(self, interface, prop, value):
		setattr(getattr(self, 'properties_'+interface.replace('.', '_')), prop, value)
		self.PropertiesChanged(interface, {prop: value}, [])

	@dbus.service.signal(dbus.PROPERTIES_IFACE, signature='sa{sv}as')
	def PropertiesChanged(self, interface, changed_props, invalidated_props):
		pass

class YMAudioView(SCVSplitView):
	def __init__(self):
		super().__init__(0, 2)

	def init(self):
		super().init()
		self.p[0].addView(MenuRecommsView())
		self.p[1].addView(ProgressView())

class YMMenuItem:
	menu_label: str

class PlaylistsView(SCLoadingSelectingListView, YMMenuItem):
	menu_label = "Плейлисты"

	# public:
	l: list[yandex_music.Playlist]

	# internal:
	to_reselect: bool

	def __init__(self, l=None):
		if (l is None): l = []
		super().__init__(l)
		self.to_reselect = True

	@staticmethod
	def _cover_uri(playlist: yandex_music.Playlist):
		if (not isinstance(playlist, yandex_music.Playlist)): return None
		return playlist.cover.uri

	@cachedfunction
	def _color(self, playlist):
		cover = self.app.get_cover(self._cover_uri(playlist), size=COVER_MIN_SIZE, delayed_view=self)
		if (not cover): return self._color._noncached(None)
		return tuple(i*1000//255 for i in cimg.pixel_color(cimg.openimg(cover)))

	def _pair(self, playlist):
		if (curses.COLORS < 9 or not curses.can_change_color()): return 0
		res = self._color(playlist)
		if (res is None): return 0
		r, g, b = res
		color = 9 #random.randrange(9, curses.COLORS)
		curses.init_color(color, r, g, b)
		pair = 2 #random.randrange(9, curses.COLORS)
		curses.init_pair(pair, color, curses.COLOR_WHITE if (max(r, g, b) < 500) else curses.COLOR_BLACK)
		return curses.color_pair(pair)

	def draw(self, stdscr):
		ret = super().draw(stdscr)
		if (not ret):
			if (self.to_reselect and not (self.l and isinstance(self.l[0], self.LoadItem))):
				self.app.selectPlayingPlaylist()
				self.to_reselect = False
		return ret

	def item(self, i):
		ret, items = super().item(i)
		if (not ret):
			if (isinstance(pl := self.l[i], yandex_music.Playlist)):
				duration = S(" · ").join(S((pl.track_count, (self.app.strfTime(pl.duration_ms/1000) if (pl.duration_ms) else None))).filter(None))

				title = S(pl.title).fit(self.width)

				roff = S(duration).fullwidth()

				spacer = ' '*(self.width - S(title).fullwidth() - roff)

				attrs = items[0][1]
				color = (self._pair(pl) if (attrs & curses.A_STANDOUT) else 0)
				if (pl.available is False): attrs |= curses.A_DIM
				dim = curses.A_DIM*(not attrs & curses.A_STANDOUT)

				items = [
					(title, attrs | color),

					(spacer, attrs | color),

					(duration, attrs | color | dim),
				]
		return (ret, items)

	def select(self):
		ret = super().select()
		if (not ret):
			try: pl = self.l[self.n]
			except IndexError: pass
			else:
				if (isinstance(pl, yandex_music.Playlist)):
					self.app.win.addView(AudiosView(playlist=pl, autoplay=True))
					ret = True
		return ret

	def load(self):
		ret = super().load()
		if (not ret):
			if (not self.l): self.l += self.app.ym.users_playlists_list()
			self.l.append(self.LoadItem(False))
		return ret

class AlbumsView(PlaylistsView):
	menu_label = "Альбомы"

	# public:
	l: list[yandex_music.Album]

	@staticmethod
	def _cover_uri(album: yandex_music.Album):
		if (not isinstance(album, yandex_music.Album)): return None
		return album.cover_uri

	def item(self, i):
		ret, items = super().item(i)
		if (not ret):
			if (isinstance(a := self.l[i], yandex_music.Album)):
				flags = ("E"*bool(a.explicit or a.content_warning)) #+ " ♥ "*self.app.is_liked(a) + " 🛇 "*self.app.is_disliked(a)).lstrip()
				duration = S(" · ").join(S((a.track_count, (self.app.strfTime(a.duration_ms/1000) if (a.duration_ms) else None))).filter(None))

				if (flags): duration = (' ' + duration)

				artist = ', '.join(a.artists_name())
				if (artist): artist += " — "
				subtitle = " · ".join(S((a.version, a.type, (a.genre if (a.type != 'podcast') else None))).filter(None))
				title = (a.title + ' '*bool(subtitle))

				roff = S(flags + duration).fullwidth()
				subtitle = S(subtitle).fit(self.width - S(artist + title).fullwidth() - roff - 1)
				artist = S(artist).fit(self.width - S(title + subtitle).fullwidth() - roff - 1)
				title = S(title).fit(self.width - S(artist + subtitle).fullwidth() - roff - 1)

				spacer = ' '*(self.width - S(artist + title + subtitle).fullwidth() - roff)

				attrs = items[0][1]
				color = (self._pair(a) if (attrs & curses.A_STANDOUT) else 0)
				if (a.available is False): attrs |= curses.A_DIM
				dim = curses.A_DIM*(not attrs & curses.A_STANDOUT)

				items = [
					(artist, attrs | color),
					(title, attrs | color | curses.A_BOLD),
					(subtitle, attrs | color | dim),

					(spacer, attrs | color),

					(flags, attrs | color | dim),
					(duration, attrs | color | dim),
				]
		return (ret, items)

	def select(self):
		ret = super().select()
		if (not ret):
			try: a = self.l[self.n]
			except IndexError: pass
			else:
				if (isinstance(a, yandex_music.Album)):
					self.app.win.addView(AudiosView(playlist=a.with_tracks(), autoplay=True))
					ret = True
		return ret

	def load(self):
		ret = super(PlaylistsView, self).load()
		if (not ret):
			if (not self.l): self.l += [i.album for i in self.app.ym.users_likes_albums()]
			self.l.append(self.LoadItem(False))
		return ret

class AudiosView(SCLoadingSelectingListView, YMMenuItem):
	menu_label = "Понравившееся"

	# public:
	playlist: '# yandex_music.Playlist'
	pl_pos_min: int

	# internal:
	to_reselect: bool

	def __init__(self, l=None, playlist=None, *, autoplay=False):
		if (l is None): l = []
		super().__init__(l)
		self.playlist, self.autoplay = playlist, autoplay
		self.to_reselect = True

	def draw(self, stdscr):
		ret = super().draw(stdscr)
		if (not ret):
			if (self.to_reselect and not (self.l and isinstance(self.l[0], self.LoadItem))):
				self.app.selectPlayingTrack()
				self.to_reselect = False
		return ret

	def key(self, c):
		if (c.ch.casefold() in 'nт'):
			try: t = self.l[self.n]
			except IndexError: pass
			else:
				for ii, (i, pl) in enumerate(self.app.play_next):
					if (i == t):
						del self.app.play_next[ii]
						self.touch()
						break
				else:
					first = c.ch.isupper()
					self.app.playNext(t, self.l, first=first)
					if (first): self.app.setPlaylist(self.l, self.n, self.pl_pos_min)
		elif (c == 'k' or c == 'л'):
			self.highlightAndScroll(random.randrange(len(self.l)-1))
		elif (c == 'b' or c == 'и'):
			self.app.selectPlayingTrack()
		elif (c == 'd' or c == 'в'):
			try: t = self.l[self.n]
			except IndexError: pass
			else:
				curses.def_prog_mode()
				curses.endwin()
				url = self.app.get_url(t)
				print(f"Downloading: {url}")
				os.system(f"""wget {repr(url)} -O {repr(f"{', '.join(t.artists_name())} - {t.title}.mp3")} -q --show-progress""")
				curses.reset_prog_mode()
		elif (c == 'l' or c == 'д'):
			try: t = self.l[self.n]
			except IndexError: pass
			else:
				if (t.lyrics_available): self.app.win.addView(LyricsView(t.get_supplement().lyrics))
		else: return super().key(c)
		return True

	@staticmethod
	def _cover_uri(track: yandex_music.Track):
		if (not isinstance(track, yandex_music.Track)): return None
		return track.cover_uri

	@cachedfunction
	def _color(self, track):
		cover = self.app.get_cover(self._cover_uri(track), size=COVER_MIN_SIZE, delayed_view=self)
		if (not cover): return self._color._noncached(None)
		return tuple(i*1000//255 for i in cimg.pixel_color(cimg.openimg(cover)))

	def _pair(self, track):
		if (curses.COLORS < 9 or not curses.can_change_color()): return 0
		res = self._color(track)
		if (res is None): return 0
		r, g, b = res
		color = 9#random.randrange(9, curses.COLORS)
		curses.init_color(color, r, g, b)
		pair = 2#random.randrange(9, curses.COLORS)
		curses.init_pair(pair, color, curses.COLOR_WHITE if (max(r, g, b) < 500) else curses.COLOR_BLACK)
		return curses.color_pair(pair)

	def is_empty(self, i):
		ret = super().is_empty(i)
		if (not ret):
			try: t = self.l[i]
			except IndexError: pass
			else:
				return (isinstance(t, yandex_music.Track) and t.available is False)
		return ret

	def item(self, i):
		ret, items = super().item(i)
		if (not ret):
			if (isinstance(t := self.l[i], yandex_music.Track)):
				pn_pos = first((ii for ii, (i, pl) in enumerate(self.app.play_next, 1) if i == t), default='')

				queue = ((str(pn_pos).join('()') + ' ') if (pn_pos) else '')
				flags = ("E"*bool(t.explicit or t.content_warning) + " ♥ "*self.app.is_liked(t) + " 🛇 "*self.app.is_disliked(t)).lstrip()
				duration = (self.app.strfTime(t.duration_ms/1000) if (t.duration_ms) else '')

				if (flags): duration = (' ' + duration)

				artist = ', '.join(t.artists_name())
				if (artist): artist += " — "
				subtitle = (t.version or '')
				title = (t.title + ' '*bool(subtitle))

				roff = S(queue + flags + duration).fullwidth()
				subtitle = S(subtitle).fit(self.width - S(artist + title).fullwidth() - roff - 1)
				artist = S(artist).fit(self.width - S(title + subtitle).fullwidth() - roff - 1)
				title = S(title).fit(self.width - S(artist + subtitle).fullwidth() - roff - 1)

				spacer = ' '*(self.width - S(artist + title + subtitle).fullwidth() - roff)

				attrs = items[0][1]
				color = (self._pair(t) if (attrs & curses.A_STANDOUT) else 0)
				if (t.available is False): attrs |= curses.A_DIM
				dim = curses.A_DIM*(not attrs & curses.A_STANDOUT)

				items = [
					(artist, attrs | color),
					(title, attrs | color | curses.A_BOLD),
					(subtitle, attrs | color | dim),

					(spacer, attrs | color),

					(queue, attrs | color),
					(flags, attrs | color | dim),
					(duration, attrs | color),
				]
		return (ret, items)

	def select(self):
		ret = super().select()
		if (not ret):
			try: t = self.l[self.n]
			except IndexError: pass
			else:
				if (isinstance(t, yandex_music.Track)):
					self.app.setPlaylist(self.l, self.n, self.pl_pos_min)
					self.app.playTrack()
					ret = True
				elif (isinstance(t, yandex_music.Playlist)):
					self.app.win.addView(AudiosView(playlist=pl, autoplay=True))
					ret = True
		return ret

	def load(self):
		ret = super().load()
		if (not ret):
			if (not self.l):
				if (self.playlist is None): self.playlist = self.app.favourites
				try: tracks = sum(self.playlist.volumes, start=[])
				except AttributeError: tracks = self.playlist.fetch_tracks()
				self.l += [(i.track if (isinstance(i, yandex_music.TrackShort)) else i) or i.fetch_track() for i in tracks]
				if (self.autoplay and len(self.l) == 1): self.select(); self.autoplay = False
				self.l.append(self.LoadItem(False))
		return ret

	def reload(self, cleared: bool = False):
		super().reload(cleared)
		self.playlist = None
		self.app.clear_cache()

class ArtistsView(SCSelectingListView):
	@staticmethod
	def _cover_uri(artist: yandex_music.Artist):
		if (not isinstance(artist, yandex_music.Artist)): return None
		return artist.cover.uri

	def item(self, i):
		ret, items = super().item(i)
		if (not ret):
			if (isinstance(a := self.l[i], yandex_music.Artist)):
				flags = '' #" ♥ "*self.app.is_liked(a) + " 🛇 "*self.app.is_disliked(a)).lstrip()
				duration = str(max(a.counts.to_dict().values()))

				if (flags): duration = (' ' + duration)

				artist = (a.name + ' ')
				subtitle = " · ".join(a.genres)

				roff = S(flags + duration).fullwidth()
				subtitle = S(subtitle).fit(self.width - S(artist).fullwidth() - roff - 1)
				artist = S(artist).fit(self.width - S(subtitle).fullwidth() - roff - 1)

				spacer = ' '*(self.width - S(artist + subtitle).fullwidth() - roff)

				attrs = items[0][1]
				color = (self._pair(a) if (attrs & curses.A_STANDOUT) else 0)
				if (a.available is False): attrs |= curses.A_DIM
				dim = curses.A_DIM*(not attrs & curses.A_STANDOUT)

				items = [
					(artist, attrs | color),
					(subtitle, attrs | color | dim),

					(spacer, attrs | color),

					(flags, attrs | color | dim),
					(duration, attrs | color | dim),
				]
		return (ret, items)

class SearchView(AudiosView, AlbumsView, ArtistsView, YMMenuItem):
	class SearchPopupView(SCView):
		transparent = True

		class SearchBox(curses.textpad.Textbox):
			def __init__(self, *args, complete=None, **kwargs):
				super().__init__(*args, **kwargs)
				if (complete is not None): self.complete = complete
				self.result = str()

			def _insert_printable_char(self, ch: SCKey):
				self.result += ch.ch
				self._update_max_yx()
				y, x = self.win.getyx()
				backyx = None
				while (y < self.maxy or x < self.maxx):
					if (self.insert_mode): oldch = SCKey(self.win.inch())
					try: self.win.addch(ch.ch)
					except curses.error: pass
					if (not self.insert_mode or not oldch.ch.isprintable()): break
					ch = oldch
					y, x = self.win.getyx()
					if (backyx is None): backyx = y, x
				if (backyx is not None): self.win.move(*backyx)

			def complete(self, result):
				return result

			def do_command(self, ch):
				self.lastcmd = ch
				ch = SCKey(ch)

				self._update_max_yx()
				y, x = self.win.getyx()

				if (ch == curses.ascii.SOH): # ^A
					self.win.move(y, 0)
				elif (ch in (curses.ascii.STX, curses.KEY_LEFT, curses.ascii.BS, curses.ascii.DEL, curses.KEY_BACKSPACE)):
					self.result = self.result[:-1]
					if (x > 0): self.win.move(y, x-1)
					elif (y == 0): pass
					elif (self.stripspaces): self.win.move(y-1, self._end_of_line(y-1))
					else: self.win.move(y-1, self.maxx)
					if (ch in (curses.ascii.BS, curses.ascii.DEL, curses.KEY_BACKSPACE)): self.win.delch()
				elif (ch == curses.ascii.EOT): # ^D
					self.win.delch()
				elif (ch == curses.ascii.ENQ): # ^E
					if (self.stripspaces): self.win.move(y, self._end_of_line(y))
					else: self.win.move(y, self.maxx)
				elif (ch in (curses.ascii.ACK, curses.KEY_RIGHT)): # ^F
					if (x < self.maxx): self.win.move(y, x+1)
					elif (y == self.maxy): pass
					else: self.win.move(y+1, 0)
				elif (ch == curses.ascii.BEL): # ^G
					return 0
				elif (ch == curses.ascii.NL): # ^J
					if (self.maxy == 0): return 0
					elif (y < self.maxy): self.win.move(y+1, 0)
				elif (ch == curses.ascii.VT): # ^K
					if (x == 0 and self._end_of_line(y) == 0): self.win.deleteln()
					else:
						self.win.move(y, x)
						self.win.clrtoeol()
				elif (ch == curses.ascii.FF): # ^L
					self.win.refresh()
				elif (ch in (curses.ascii.SO, curses.KEY_DOWN)): # ^N
					if (y < self.maxy):
						self.win.move(y+1, x)
						if (x > self._end_of_line(y+1)): self.win.move(y+1, self._end_of_line(y+1))
				elif (ch == curses.ascii.SI): # ^O
					self.win.insertln()
				elif (ch in (curses.ascii.DLE, curses.KEY_UP)): # ^P
					if (y > 0):
						self.win.move(y-1, x)
						if (x > self._end_of_line(y-1)): self.win.move(y-1, self._end_of_line(y-1))
				elif (ch in (curses.ascii.ESC, curses.KEY_EXIT)):
					self.result = ''
					return 0
				elif (ch == curses.ascii.TAB):
					self.set(self.complete(self.result))
				elif (ch.ch.isprintable()):
					if (y < self.maxy or x < self.maxx): self._insert_printable_char(ch)

				return 1

			def set(self, s):
				self.win.move(0, 0)
				self.win.clrtoeol()

				self.result = ''
				for i in s:
					self._insert_printable_char(SCKey(i))

				self.win.refresh()

			def edit(self, validate=None):
				while (True):
					try: ch = self.win.get_wch()
					except curses.error: continue # TODO FIXME
					if (validate): ch = validate(ch)
					if (not ch): continue
					if (not self.do_command(ch)): break
					self.win.refresh()

				return self.result

		SEARCH = "Поиск"
		QUERY = "Запрос:"

		def __init__(self, callback: callable):
			super().__init__()
			self.callback = callback

		def draw(self, stdscr):
			ret = super().draw(stdscr)
			if (not ret):
				eh, ew = 5, 48
				ey, ex = (self.height-eh)//2, (self.width-ew)//2
				ep = curses.newwin(eh, ew, ey, ex)
				ep.addstr(0, 0, '╭'+'─'*(ew-2)+'╮')
				for i in range(1, eh-1): ep.addstr(i, 0, '│'+' '*(ew-2)+'│')
				ep.addstr(eh-2, 0, '╰'+'─'*(ew-2)+'╯')
				ep.addstr(1, 2, self.SEARCH.center(ew-4))
				ep.addstr(2, 2, self.QUERY)
				ep.refresh()
				y, x = stdscr.getbegyx()
				search = self.SearchBox(curses.newwin(y+1, x+ew-4-len(self.QUERY), ey+2, ex+3+len(self.QUERY)), complete=self.app.search_complete)
				query = search.edit().strip()
				self.callback(query)
				self.die()
				ret = True
			return ret

	menu_label = "Поиск"

	search: '# yandex_music.Search'
	query: str

	def __init__(self, l=None, search=None):
		if (l is None): l = []
		super().__init__(l)
		self.search = search

	def init(self):
		super().init()
		self.app.win.addView(self.SearchPopupView(self.set_query))
		self.to_load = False

	def set_query(self, query: str):
		if (not query):
			self.die()
			return
		self.query = query
		self.to_load = True

	def load(self):
		ret = SCLoadingSelectingListView.load(self)
		if (not ret):
			l = []
			try:
				if (self.search is None): self.search = self.app.ym.search(self.query)
				#else: self.search = self.search.next_page() # TODO FIXME: TypeError
			except (yandex_music.exceptions.BadRequestError): pass
			else:
				if (self.search.best is not None): l += [self.search.best.result, self.EmptyItem()]
				if (self.search.playlists is not None): l += [i for i in self.search.playlists.results if i not in l]
				if (self.search.albums is not None): l += [i for i in self.search.albums.results if i not in l]
				if (self.search.tracks is not None): l += [i for i in self.search.tracks.results if i not in l]

			self.l += l
			self.l.append(self.LoadItem(False)) #bool(l))) # TODO FIXME
		return ret

	def reload(self, cleared: bool = False):
		super().reload(cleared)
		self.search = None

class MenuRecommsView(AudiosView):
	station = 'user:onmywave'
	from_ = 'user-onyourwave'

	# public:
	batch_id: str

	def __init__(self, l=None):
		if (l is None): l = [
			AudiosView,
			PlaylistsView,
			AlbumsView,
			SearchView,
			self.EmptyItem(),
		]
		super().__init__(l)
		self.pl_pos_min = len(self.l)

	def item(self, i):
		ret, items = super().item(i)
		if (not ret):
			if (isinstance(t := self.l[i], type) and issubclass(t, YMMenuItem)):
				attrs = items[0][1]
				text = S(f" * {t.menu_label}").fit(self.width)
				items = [(text, attrs)]
		return (ret, items)

	def select(self):
		ret = super(AudiosView, self).select()
		if (not ret):
			try: t = self.l[self.n]
			except IndexError: pass
			else:
				if (self.is_empty(self.n)): pass
				elif (isinstance(t, type) and issubclass(t, YMMenuItem)):
					self.app.win.addView(t())
					ret = True
				elif (isinstance(t, yandex_music.Track)):
					if (self.app.playlist is not self.l): self.app.ym.rotor_station_feedback_radio_started(self.station, self.from_)
					self.app.setPlaylist(self.l, self.n, self.pl_pos_min, station=(self.station, self.batch_id))
					self.app.playTrack()
					ret = True
		return ret

	def load(self):
		ret = super(AudiosView, self).load()
		if (not ret):
			r = self.app.ym.rotor_station_tracks(self.station, queue=self.app.track.track_id if (self.app.track) else None)

			for i in r.sequence:
				if (i.track not in self.l): self.l.append(i.track)

			self.l.append(self.LoadItem(True))

			self.batch_id = r.batch_id
		return ret

	def reload(self, cleared: bool = False):
		del self.l[self.pl_pos_min:]
		super().reload(True)
		self.n = self.pl_pos_min

class LyricsView(SCView):
	transparent = True

	text: str
	t: int

	def __init__(self, lyrics: yandex_music.Lyrics):
		super().__init__()
		self.lyrics = lyrics

	def init(self):
		super().init()
		self.text = S(self.lyrics.full_lyrics)

	def draw(self, stdscr):
		ret = super().draw(stdscr)
		if (not ret):
			eh, ew = self.height-2, self.width-4
			self.t = max(0, min(self.t, self.text.wrap(ew-3).count('\n') - eh+4))
			ep = stdscr.subpad(eh, ew, 2, 2)

			ep.addstr(0, 0, '╭'+'─'*(ew-2)+'╮')
			for i in range(1, eh-1): ep.addstr(i, 0, '│'+' '*(ew-2)+'│')
			ep.addstr(eh-2, 0, '╰'+'─'*(ew-2)+'╯')

			text = self.text.wrap(ew-3)
			for ii, i in enumerate(text.split('\n')[self.t:eh-3 + self.t]):
				ep.addstr(ii+1, 2, i)
		return ret

	def key(self, c):
		if (c == curses.KEY_UP):
			self.t -= 1
			self.touch()
		elif (c == curses.KEY_DOWN):
			self.t += 1
			self.touch()
		else: return super().key(c)
		return True

class ProgressView(SCView):
	def __init__(self):
		super().__init__()
		self.paused = bool()
		self.repeat = bool()
		self.tm = str()

	def init(self):
		self.tm = time.strftime('%X')

	def proc(self):
		ret = super().proc()
		if (not ret):
			paused = (not self.app.p.is_playing() and self.app.p.will_play())
			repeat = self.app.repeat
			tm = time.strftime('%X')

			if ((paused, repeat, tm) != (self.paused, self.repeat, self.tm)):
				self.paused, self.repeat, self.tm = paused, repeat, tm
				self.touch()
		return ret

	def draw(self, stdscr):
		ret = super().draw(stdscr)
		if (not ret):
			pl = self.app.p.get_length()
			pt = max(0, self.app.p.get_time())
			pp = min(1, self.app.p.get_position())
			pgrstr = (self.app.strfTime(pt/1000), (self.app.strfTime(max(0, pl)/1000) if (pl != 0) else '--:--'), self.tm)
			icons = '↺'*self.repeat
			if (icons): icons = ' '+icons
			stdscr.addstr(0, 1, S(self.app.trackline).cyclefit(self.width-2 - len(icons), self.app.tl_rotate, start_delay=10).ljust(self.width-2 - len(icons))+icons, curses.A_UNDERLINE)
			stdscr.addstr(1, 1, pgrstr[0], curses.A_BLINK*self.paused)
			stdscr.addstr(1, 1+len(pgrstr[0]), '/'+pgrstr[1]+' │')
			stdscr.addstr(1, 4+len(str().join(pgrstr[:2])), Progress.format_bar(pp, 1, self.width - len(str().join(pgrstr))-4, border=''), curses.color_pair(1))
			stdscr.addstr(1, self.width-2 - len(pgrstr[-1]), '▏'+pgrstr[-1])
			self.touch()
		return ret

class LoginView(SCView):
	AUTHORIZATION = "Авторизация (Яндекс)"
	LOGIN = "Логин:"
	PASSWORD = "Пароль:"
	CAPTCHA = "Капча:"
	ERROR = "Неизвестная ошибка"

	transparent = True

	def __init__(self, callback=None):
		super().__init__()
		self.callback = callback or noop

	class TextBox(curses.textpad.Textbox):
		def set(self, s):
			self.do_command(curses.ascii.VT)

			for i in s:
				self._insert_printable_char(ord(i))

			self.win.refresh()

	class PasswordBox(TextBox):
		def __init__(self, *args, **kwargs):
			super().__init__(*args, **kwargs)
			self.result = str()

		def _insert_printable_char(self, ch):
			self.result += chr(ch)
			return super()._insert_printable_char('*')

		def do_command(self, ch):
			if (ch in (curses.ascii.STX, curses.KEY_LEFT, curses.ascii.BS, curses.KEY_BACKSPACE)): self.result = self.result[:-1]
			return super().do_command(ch)

		def set(self, s):
			self.result = ''
			super().set(s)

		def gather(self):
			return self.result

	def draw(self, stdscr):
		global ym_login, ym_pw

		ret = super().draw(stdscr)
		if (not ret):
			cap = None

			l, p = ym_login, ub64(ym_pw)
			try: t = self.app.auth.get_token(l, p)
			except YMAuthCaptcha as ex: cap = ex
			except YMAuthError: pass
			else:
				if (t is not None):
					self.app.set_token(t)
					self.callback()
					self.die()
					ret = True
					return ret # XXX

			eh, ew = 7, 48
			ey, ex = (self.height-eh)//2, (self.width-ew)//2

			ep = curses.newwin(eh, ew, ey, ex)
			ep.addstr(0, 0, '╭'+'─'*(ew-2)+'╮')
			for i in range(1, eh-1): ep.addstr(i, 0, '│'+' '*(ew-2)+'│')
			ep.addstr(eh-2, 0, '╰'+'─'*(ew-2)+'╯')
			ep.addstr(1, 2, self.AUTHORIZATION.center(ew-4))
			ep.addstr(2, 2, self.LOGIN)
			ep.addstr(3, 2, self.PASSWORD)
			ep.addstr(4, 2, self.CAPTCHA, curses.A_DIM*(cap is None))
			ep.refresh()

			y, x = stdscr.getbegyx()
			login = self.TextBox(curses.newwin(y+1, x+ew-4-len(self.LOGIN), ey+2, ex+3+len(self.LOGIN)))
			login.set(l)
			password = self.PasswordBox(curses.newwin(y+1, x+ew-4-len(self.PASSWORD), ey+3, ex+3+len(self.PASSWORD)))
			password.set(p)
			captcha = self.TextBox(curses.newwin(y+1, x+ew-4-len(self.CAPTCHA), ey+4, ex+3+len(self.CAPTCHA)))

			while (True):
				ep.addstr(2, 2, self.LOGIN, curses.A_BOLD); ep.refresh()
				try: l = login.edit().strip()
				finally: ep.addstr(2, 2, self.LOGIN)

				ep.addstr(3, 2, self.PASSWORD, curses.A_BOLD); ep.refresh()
				try: p = password.edit().strip()
				finally: ep.addstr(3, 2, self.PASSWORD)

				ep.addstr(4, 2, self.CAPTCHA, curses.A_BOLD if (cap is not None) else curses.A_DIM); ep.refresh()
				if (cap is not None):
					webbrowser.open(cap.image_url)
					try: c = captcha.edit().strip()
					finally: ep.addstr(4, 2, self.CAPTCHA)
					cap = cap.with_answer(c)

				try: t = self.app.auth.get_token(l, p, cap)
				except YMAuthError as ex:
					if (isinstance(ex, YMAuthCaptcha)): cap = ex
					else: raise
					ep.addstr(1, 2, S(str(ex) or self.ERROR).fit(ew-4).center(ew-4))
					continue
				else:
					if (t is not None):
						ym_login, ym_pw = l, b64(p)
						break

			self.app.set_token(t)
			self.callback()
			self.die()
			ret = True
		return ret

class HelpView(SCView):
	transparent = True

	def draw(self, stdscr):
		ret = super().draw(stdscr)
		if (not ret):
			eh, ew = 18, 40
			ep = stdscr.subpad(eh, ew, (self.height-eh)//2, (self.width-ew)//2)
			ep.addstr(0, 0, '╭'+'─'*(ew-2)+'╮')
			for i in range(1, eh-1): ep.addstr(i, 0, '│'+' '*(ew-2)+'│')
			ep.addstr(eh-2, 0, '╰'+'─'*(ew-2)+'╯')
			for ii, i in enumerate("""\
	   YMAudio: Help
q, esc, bspace — back
r — toggle repeat
p — toggle pause
a — next track
s — stop
d — download track using wget
h — help
k — select random track
z — previous track
b — select playing track
n — enqueue track
left/right, nums — seek
/, ^F — find
^L — force redraw""".split('\n')):
				ep.addstr(ii+1, 2, i)
		return ret

	def key(self, c):
		self.die()
		#else: return super().key(c)
		return True

class FindView(SCView): # TODO: more intuitive control?
	transparent = True

	prompt = "/"

	def __init__(self):
		super().__init__()
		self.q = self.prompt

	def init(self):
		super().init()
		self.app.top.focus = 1

	def die(self):
		super().die()
		self.app.top.focus = 0

	def draw(self, stdscr):
		ret = super().draw(stdscr)
		if (not ret):
			stdscr.addstr(0, 0, self.q.ljust(self.width))
		return ret

	def key(self, c):
		if (c in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT)):
			self.app.win.top.key(c)
		elif (c == curses.ascii.DEL or c == curses.ascii.BS or c == curses.KEY_BACKSPACE):
			self.q = self.q[:-1]
			self.touch()
			if (not self.q):
				self.app.waitkeyrelease(c)
				self.die()
		elif (c == curses.ascii.NL or c == curses.ascii.ESC or c == curses.KEY_EXIT):
			if (c == curses.ascii.NL): self.app.win.top.key(c)
			self.die()
		elif (c.ch.isprintable()):
			self.q += c.ch
			self.touch()

			q = self.q.removeprefix(self.prompt)
			ci = q.islower()
			for ii, t in enumerate(self.app.win.top.l[self.app.win.top.n:]): # FIXME
				if (isinstance(t, yandex_music.Track) and any(q in (i.casefold() if (ci) else i) for i in (*t.artists_name(), t.title))):
					self.app.win.top.highlightAndScroll(self.app.win.top.n + ii)
					break
		else: return super().key(c)
		return True

class QuitView(SCView):
	transparent = True

	l, t = (), int()

	def draw(self, stdscr):
		ret = super().draw(stdscr)
		if (not ret):
			eh, ew = 8, 23
			ep = stdscr.subpad(eh, ew, (self.height-eh)//2, (self.width-ew)//2)
			ep.addstr(0, 0, '╭'+'─'*(ew-2)+'╮')
			for i in range(1, eh-1): ep.addstr(i, 0, '│'+' '*(ew-2)+'│')
			ep.addstr(eh-2, 0, '╰'+'─'*(ew-2)+'╯')
			for ii, i in enumerate("Are you sure you\nwant to exit?\nPress back again to\nexit or select to\nstay in YMAudio.".split('\n')):
				ep.addstr(1+ii, 2, i.center(ew-3), curses.A_BOLD)
		return ret

	def key(self, c):
		if (c == curses.ascii.NL):
			self.die()
		elif (c == 'q' or c == 'й' or c == curses.ascii.DEL or c == curses.ascii.BS or c == curses.ascii.ESC or c == curses.KEY_BACKSPACE or c == curses.KEY_EXIT):
			self.app.die()
		else: return super().key(c)
		return True

class App(SCApp):
	# public:
	p: vlc.MediaPlayer
	ym: yandex_music.Client
	auth: '# YMAuth'
	win: '# SCWindow'
	user_id: int
	play_next: list[tuple[yandex_music.Track, yandex_music.TracksList]]
	repeat: bool
	tl_rotate: int
	station: None

	# private:
	playlist: list
	pl_pos: int
	pl_pos_min: int
	pl_peer: int
	error: None
	clicked: bool
	dbus: None
	dbus_eventloop: None
	glib_eventloop: None
	mpris: None
	notify: None

	# internal:
	_track: dict
	_lastproc: ...
	_lastpb: ...
	_lastmd: ...
	_lastpos: ...
	_get_cover_thread: ...
	_track_download_thread: None

	# properties:
	favourites: yandex_music.TracksList
	unfavourites: yandex_music.TracksList
	track: yandex_music.Track
	trackline: str

	def init(self):
		self.mouse_delay = 0
		self.mouse_mask = (curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
		super().init()

		curses.use_default_colors()
		try: curses.init_pair(1, curses.COLOR_WHITE, 8)
		except curses.error: curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)  # fbcon

		curses.curs_set(False)
		self.stdscr.nodelay(True)
		self.stdscr.leaveok(True)

		self.auth = YMAuthWeb()

		self.p.get_instance().log_unset()
		self.p.audio_set_volume(100)

		try: self.glib_eventloop = GLib.MainLoop()
		except NameError: pass
		else:
			self.dbus_eventloop = dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
			threading.Thread(target=self.glib_eventloop.run, daemon=True).start()
			self.dbus = dbus.SessionBus()
			self.mpris = MediaPlayer2(self, dbus.service.BusName('org.mpris.MediaPlayer2.ymaudio', bus=self.dbus), '/org/mpris/MediaPlayer2')
			self.update_all()

		if (self.mpris is None):
			try: raise notify2.init('YMAudio')
			except Exception: pass
			else:
				self.notify = notify2.Notification('', icon='media-playback-start')
				self.notify.set_category('x-gnome.music')
				self.notify.set_urgency(notify2.URGENCY_LOW)
				self.notify.set_hint('action-icons', True)
				self.notify.connect('closed', noop)
				self.notify.add_action('media-skip-backward', 'Previous track', lambda *_: self.playPrevTrack())
				self.notify.add_action('media-playback-pause', 'Pause', lambda *_: self.playPause())
				self.notify.add_action('media-skip-forward', 'Next track', lambda *_: self.playNextTrack())

		self.pl_pos = -1

		self.win = self.top.p[0]

		try: self.set_token(ym_token)
		except yandex_music.exceptions.BadRequestError: pass
		if (not self.user_id): self.win.addView(LoginView())

	def die(self):
		super().die()

		try: self.stop()
		except Exception: pass

		try: self.update_all()
		except Exception: pass

	_lastproc = int()
	_lastpb = None
	_lastmd = None
	_lastpos = int()
	def proc(self):
		ret = super().proc()
		if (not ret):
			if (time.time()-self._lastproc >= 0.1):
				self._lastproc = time.time()

				pb = self.mpris.properties_org_mpris_MediaPlayer2_Player.PlaybackStatus
				if (pb != self._lastpb):
					self._lastpb = pb
					self.update_properties(PlaybackStatus=pb)

				md = self.mpris.properties_org_mpris_MediaPlayer2_Player.Metadata
				if (md != self._lastmd):
					self._lastmd = md
					self.update_properties(Metadata=md)

				pos = self.mpris.properties_org_mpris_MediaPlayer2_Player.Position
				if (abs(pos-self._lastpos) > 500*1000):
					self._lastpos = pos
					if (self.p.is_playing()): self.update_properties(Position=pos)

			state = self.p.get_state()
			if (self.p.get_length() > 0 and state == vlc.State.Ended):
				if (self._track_download_thread is None): self.playNextTrack()
			elif (state == vlc.State.Ended and self._track_download_thread is not None): self.play()
		return ret

	@staticmethod
	def strfTime(t):
		return time.strftime('%H:%M:%S', time.gmtime(t)).lstrip('0').lstrip(':')

	def set_token(self, access_token: str):
		global ym_token
		c = self.ym = yandex_music.Client(access_token)
		c.init()
		self.user_id = c.me.account.uid
		ym_token = access_token
		db.save(nolog=True)
		if (isinstance(self.win.top, MenuRecommsView)): self.win.top.load()

	def is_liked(self, track: yandex_music.Track) -> bool:
		if (not hasattr(track, 'liked')): track.liked = (track.track_id in self.favourites.tracks_ids)
		return track.liked

	def is_disliked(self, track: yandex_music.Track) -> bool:
		if (not hasattr(track, 'disliked')): track.disliked = (track.track_id in self.unfavourites.tracks_ids)
		return track.disliked

	@cachedfunction
	def search_complete(self, part: str):
		part = part.lstrip()
		return first((i for i in self.ym.search_suggest(part).suggestions if i.casefold().startswith(part.casefold()) and i.casefold() != part.casefold()), default=part)

	@cachedfunction
	def get_url(self, track: yandex_music.Track):
		return max(track.get_download_info(), key=operator.attrgetter('bitrate_in_kbps')).get_direct_link()

	@cachedfunction
	def get_lyrics(self, track: yandex_music.Track):
		if (not track.lyrics_available): return ''
		lyrics = track.get_supplement().lyrics
		if (not lyrics): return ''
		try: return lyrics.full_lyrics
		except AttributeError: return lyrics.lyrics

	_get_cover_thread = dict()
	def get_cover(self, url, *, size='orig', delayed_view=None):
		if (not url): return None

		if (not url.startswith('http')): url = f"https://{url}"
		url = url.replace('%%', size)

		if (delayed_view is None or self._get_cover.is_cached(url)): return self._get_cover(url)

		thread = self._get_cover_thread.get(url)
		if (thread is None):
			thread = self._get_cover_thread[url] = threading.Thread(target=lambda: self._get_cover(url) and delayed_view.touch(), daemon=True)
			thread.start()
		elif (not thread.is_alive()):
			del self._get_cover_thread[url]
			return self._get_cover(url)

		return None

	@cachedfunction
	def _get_cover(self, url):
		if (not url): return None
		cache_folder = os.path.expanduser("~/.cache/YMAudio/covers")
		os.makedirs(cache_folder, exist_ok=True)
		path = os.path.join(cache_folder, md5(url)+os.path.splitext(url)[1])
		if (not os.path.exists(path)):
			r = requests.get(url)
			data = r.content
			if (not data): return None
			with open(path, 'wb') as f:
				f.write(data)
		return 'file://'+os.path.abspath(path)

	@staticmethod
	def _download_track(url, path, *, path_fifo=None, done_callback=None, _stop_event):
		path_part = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.part")
		done = bool()
		try:
			with requests.get(url, stream=True) as r:
				r.raise_for_status()
				if (path_fifo is not None): buf = bytearray()
				with open(path_part, 'wb') as f, \
				     open(path_fifo, 'wb') if (path_fifo is not None) else noopcm as fifo:
					for chunk in r.iter_content(chunk_size=8192):
						if (_stop_event.is_set()): break

						f.write(chunk)

						if (noopcm is not None):
							buf += chunk
							if (buf):
								try: os.write(fifo.fileno(), buf)
								except OSError: pass
								else: del buf[:]
					else: done = True
		finally:
			if (done):
				os.rename(path_part, path)
				if (done_callback is not None): done_callback(path)
			else:
				try: os.remove(path_part)
				except OSError: pass

	def playTrack(self, t=None, *, notify=True, set_pos=True):
		if (t is None):
			r = self.playTrack(self.playlist[self.pl_pos], set_pos=False)
			if (self.station and isinstance(self.win.top, MenuRecommsView)): self.win.top.load()
			return r

		if (set_pos):
			for ii, i in enumerate(self.playlist):
				if (i == t): self.pl_pos = ii; break

		self.error = None
		self.stop()

		try:
			url = self.get_url(t)
			cache_folder = os.path.expanduser("~/.cache/YMAudio/audio")
			os.makedirs(cache_folder, exist_ok=True)
			path = os.path.join(cache_folder, (self._trackline(t) + (os.path.splitext(url)[1] or '.mp3')))
			if (not os.path.exists(path)):
				if (thread := self._track_download_thread): thread.stop()
				path_fifo = "/tmp/.YMAudio.vlc.fifo"
				if (not os.path.exists(path_fifo)): os.mkfifo(path_fifo)
				def _done_cb(path):
					pos = self.p.get_time()
					self.p.set_mrl(path)
					self.p.play()
					self.p.set_time(pos)
				thread = self._track_download_thread = StoppableThread(target=self._download_track, args=(url, path), kwargs={'path_fifo': path_fifo, 'done_callback': _done_cb}, daemon=True)
				thread.start()
				path = path_fifo
			self.p.set_mrl(path)
			self.play()
		except Exception as ex:
			if (isinstance(ex, yandex_music.exceptions.BadRequestError) and ex.name == 'session-expired'): self.user_id = None
			self.error = ex
			return False

		self.track = t

		self.tl_rotate = 0
		self.selectPlayingTrack()
		if (notify): self.notifyPlaying(t)
		if (self.station): self.ym.rotor_station_feedback_track_started(self.station[0], self.track.track_id, self.station[1])

		return True

	def playNextTrack(self, force_next=False):
		if (self.station and self.track): (self.ym.rotor_station_feedback_skip if (force_next) else self.ym.rotor_station_feedback_track_finished)(self.station[0], self.track.track_id, self.p.get_position()/1000, self.station[1])

		if (self.play_next):
			t, pl = self.play_next.pop(0)
			self.setPlaylist(pl)
			self.playTrack(t)
			return

		if (self.repeat and not force_next):
			self.playTrack(self.track, notify=False)
			return

		if (not self.playlist):
			if (not isinstance(self.win.top, AudiosView)): return
			self.setPlaylist(self.win.top.l, pos_min=self.win.top.pl_pos_min)
		else: self.pl_pos = max((self.pl_pos+1) % (len(self.playlist)-1), self.pl_pos_min)

		while (self.pl_pos < len(self.playlist)-1):
			t = self.playlist[self.pl_pos]
			if (isinstance(t, SCSelectingListView.EmptyItem) or isinstance(t, yandex_music.Track) and t.available is False): self.pl_pos += 1; continue
			break

		self.playTrack()

	def playPrevTrack(self):
		if (not self.playlist):
			if (not isinstance(self.win.top, AudiosView)): return
			self.setPlaylist(self.win.top.l, pos_min=self.win.top.pl_pos_min)
		elif (self.pl_pos > 0): self.pl_pos = max(self.pl_pos-1, self.pl_pos_min)

		while (self.pl_pos > 0):
			t = self.playlist[self.pl_pos]
			if (isinstance(t, SCSelectingListView.EmptyItem) or isinstance(t, yandex_music.Track) and t.available is False): self.pl_pos -= 1; continue
			break

		self.playTrack()

	def selectPlaying(self, x):
		for ii, i in enumerate(self.win.top.l):
			if (i == x):
				self.win.top.setSelection(ii)
				self.win.top.highlightAndScroll(ii)
				break

	def selectPlayingTrack(self):
		if (isinstance(self.win.top, AudiosView)):
			self.selectPlaying(self.track)

	def selectPlayingPlaylist(self):
		if (isinstance(self.win.top, PlaylistsView)):
			self.selectPlaying(self.playlist)

	def play(self):
		self.p.play()
		self.update_properties('PlaybackStatus', 'Metadata', 'Position')
		self.top.p[1].top.touch()

	def pause(self):
		self.p.pause()
		self.update_properties('PlaybackStatus')
		self.top.p[1].top.touch()

	def playPause(self):
		self.p.pause()
		self.update_properties('PlaybackStatus')
		self.top.p[1].top.touch()

	def stop(self):
		self.p.stop()

		if (self.notify is not None): self.notify.close()
		if (self._track_download_thread is not None): self._track_download_thread.stop()
		if (self.station and self.track): self.ym.rotor_station_feedback_track_finished(self.station[0], self.track.track_id, self.p.get_position()/1000, self.station[1])

		self.track = None
		self.update_properties('PlaybackStatus', 'Metadata')

		self.win.top.unselect()
		self.win.top.touch()
		if (self.views): self.top.p[1].top.touch()

	def setPosition(self, position):
		if (not self.p.is_playing()): return
		self.p.set_position(position)
		self.update_properties('Position')
		self.top.p[1].top.touch()

	def setPlaylist(self, playlist, pos=-1, pos_min=0, *, station=None):
		self.playlist = playlist
		self.pl_pos_min = pos_min
		self.pl_pos = max(pos, pos_min)
		self.station = station

	def playNext(self, t: yandex_music.Track, pl: yandex_music.TracksList, *, first=False):
		if (first): self.play_next.insert(0, (t, pl))
		else: self.play_next.append((t, pl))
		self.win.top.touch()

	def toggleRepeat(self):
		self.repeat = not self.repeat
		self.update_properties('LoopStatus')
		self.top.p[1].top.touch()

	def seekRew(self):
		self.setPosition(self.p.get_position()-0.01)

	def seekFwd(self):
		self.setPosition(self.p.get_position()+0.01)

	def notifyPlaying(self, t):
		try:
			self.notify.update(t['title'], t['artist'])
			self.notify.show()
		except Exception: pass

	def update_properties(self, *invalidated_props, **changed_props):
		o = self.mpris.properties_org_mpris_MediaPlayer2_Player
		changed_props.update({i: (lambda v: v.fget(o) if (isinstance(v, property)) else v)(getattr(o, i)) for i in invalidated_props if i not in changed_props})
		self.mpris.PropertiesChanged('org.mpris.MediaPlayer2.Player', changed_props, [])

	def update_all(self):
		self.mpris.PropertiesChanged('org.mpris.MediaPlayer2.Player', self.mpris.properties_org_mpris_MediaPlayer2_Player.to_dict(), [])

	def clear_cache(self):
		try: del self.favourites
		except AttributeError: pass

		try: del self.unfavourites
		except AttributeError: pass

	def like(self, track: yandex_music.Track):
		r = self.ym.users_likes_tracks_add(track.track_id)
		self.clear_cache()
		return r

	def unlike(self, track: yandex_music.Track):
		r = self.ym.users_likes_tracks_remove(track.track_id)
		self.clear_cache()
		return r

	def dislike(self, track: yandex_music.Track):
		r = self.ym.users_dislikes_tracks_add(track.track_id)
		self.clear_cache()
		return r

	def undislike(self, track: yandex_music.Track):
		r = self.ym.users_dislikes_tracks_remove(track.track_id)
		self.clear_cache()
		return r

	@cachedproperty
	def favourites(self) -> yandex_music.TracksList:
		return self.ym.users_likes_tracks()

	@cachedproperty
	def unfavourites(self) -> yandex_music.TracksList:
		return self.ym.users_dislikes_tracks()

	@property
	def track(self) -> yandex_music.Track:
		return self._track

	@track.setter
	def track(self, track: yandex_music.Track):
		self._track = track
		self.update_properties('Metadata')

	@staticmethod
	def _trackline(track: yandex_music.Track) -> str:
		artist = ', '.join(track.artists_name())
		if (artist): artist += " — "
		return (artist + track.title)

	@property
	def trackline(self) -> str:
		if (self.error is not None): return f"Error: {self.error}"
		t = self.track
		if (not t): return ''
		self.tl_rotate += 1
		return self._trackline(t)

app = App(proc_rate=10)

@app.onkey('q')
@app.onkey('й')
@app.onkey(curses.ascii.BS)
@app.onkey(curses.ascii.DEL)
@app.onkey(curses.ascii.ESC)
@app.onkey(curses.KEY_BACKSPACE)
@app.onkey(curses.KEY_EXIT)
def back(self, c):
	if (len(self.win.views) <= 1): self.win.addView(QuitView())
	else: self.win.top.die()

@app.onkey('h')
@app.onkey('р')
@app.onkey(curses.KEY_F1)
def help(self, c):
	self.win.addView(HelpView())

@app.onkey(curses.KEY_F5)
def reload(self, c):
	if (self.win.views and isinstance(self.win.top, SCLoadingListView)):
		self.win.top.reload()

@app.onkey(curses.KEY_LEFT)
def rew(self, c):
	self.seekRew()

@app.onkey(curses.KEY_RIGHT)
def fwd(self, c):
	self.seekFwd()

@app.onkey('1')
@app.onkey('2')
@app.onkey('3')
@app.onkey('4')
@app.onkey('5')
@app.onkey('6')
@app.onkey('7')
@app.onkey('8')
@app.onkey('9')
@app.onkey('0')
def seek(self, c):
	self.setPosition(0.1*('1234567890'.index(c.ch)))

@app.onkey(' ')
@app.onkey('p')
@app.onkey('з')
def pause(self, c):
	self.playPause()

@app.onkey('a')
@app.onkey('ф')
def next(self, c):
	self.playNextTrack(force_next=True)

@app.onkey('z')
@app.onkey('я')
def prev(self, c):
	self.playPrevTrack()

@app.onkey('s')
@app.onkey('ы')
def stop(self, c):
	self.stop()
	self.setPlaylist([])

@app.onkey('r')
@app.onkey('к')
def repeat(self, c):
	self.toggleRepeat()

@app.onkey('+')
def like(self, c):
	if (isinstance(self.win.top, AudiosView)):
		if (self.win.top.l and isinstance(t := self.win.top.l[self.win.top.n], yandex_music.Track)):
			if (not self.is_liked(t)): t.liked = self.like(t)
			else: t.liked = not self.unlike(t)
			t.disliked = False  # resets in both cases

			if (self.station and isinstance(self.win.top, MenuRecommsView)): self.win.top.load()
			self.win.top.touch()

@app.onkey('=')
def like_and_play(self, c):
	if (isinstance(self.win.top, AudiosView)):
		if (self.win.top.l and isinstance(t := self.win.top.l[self.win.top.n], yandex_music.Track)):
			t.liked = self.like(t)
			t.disliked = False  # resets in both cases

			if (self.station and isinstance(self.win.top, MenuRecommsView)): self.win.top.load()

			if (self.win.top.s != self.win.top.n): self.win.top.select()
			self.win.top.touch()

@app.onkey('-')
def dislike(self, c):
	if (isinstance(self.win.top, AudiosView)):
		if (self.win.top.l and isinstance(t := self.win.top.l[self.win.top.n], yandex_music.Track)):
			if (not self.is_disliked(t)): t.disliked = self.dislike(t)
			else: t.disliked = not self.undislike(t)
			t.liked = False  # resets in both cases

			if (self.station and isinstance(self.win.top, MenuRecommsView)): self.win.top.load()
			self.win.top.touch()

@app.onkey('_')
def dislike_and_skip(self, c):
	if (isinstance(self.win.top, AudiosView)):
		dislike(self, c)
		self.playNextTrack(force_next=True)

@app.onkey('/')
@app.onkey('.')
@app.onkey('^F')
@app.onkey(curses.KEY_FIND)
def find(self, c):
	self.top.p[1].addView(FindView())

@app.onkey('^L')
def redraw(self, c):
	self.touchAll()
	self.stdscr.redrawwin()

@app.onkey(curses.KEY_MOUSE)
def mouse(self, c):
	try: id, x, y, z, bstate = curses.getmouse()
	except (curses.error, IndexError): return
	if (not self.win.views): return

	height, width = self.stdscr.getmaxyx()

	if (y < height-2):
		if (bstate == curses.BUTTON4_PRESSED):
			self.win.top.t = max(self.win.top.t-3, 0)
			self.win.top.touch()
		elif (bstate == curses.REPORT_MOUSE_POSITION or bstate == 2097152):
			if (len(getattr(self.win.top, 'l', ())) > height):
				self.win.top.t = min(self.win.top.t+3, len(self.win.top.l) - height+2)
				self.win.top.touch()
			elif (isinstance(self.win.top, LyricsView)):
				self.win.top.t += 3
				self.win.top.touch()
		elif (bstate == curses.BUTTON1_PRESSED):
			if (isinstance(qw := self.win.top, QuitView)): self.key(SCKey('\n')); return
			n = (self.win.top.t + y)
			if (not self.win.top.is_empty(n)):
				self.win.top.n = n
				if (time.time() < self.clicked): self.win.top.select(); self.clicked = True
				self.win.top.touch()
		elif (bstate == curses.BUTTON1_RELEASED):
			self.clicked = False if (self.clicked == True) else (time.time() + 0.2)
		elif (bstate == curses.BUTTON3_PRESSED):
			self.key(SCKey(curses.KEY_EXIT))
	elif (y == height-2 and x >= width-2):
		if (bstate == curses.BUTTON1_PRESSED): self.toggleRepeat()
	elif (y == height-1):
		if (x < 14):
			if (bstate in (curses.BUTTON1_PRESSED, curses.BUTTON3_PRESSED, curses.BUTTON3_RELEASED)):
				self.pause()
			elif (bstate == curses.BUTTON4_PRESSED):
				self.playPrevTrack()
			elif (bstate == curses.REPORT_MOUSE_POSITION or bstate == 2097152):
				self.playNextTrack()

		elif (x <= width-12):
			if (bstate == curses.BUTTON1_PRESSED):
				self.setPosition((x-14)/(width-12-14+1))
			elif (bstate == curses.BUTTON4_PRESSED):
				self.seekRew()
			elif (bstate == curses.REPORT_MOUSE_POSITION or bstate == 2097152):
				self.seekFwd()

def main():
	global app
	setproctitle.setproctitle('YMAudio')
	db.load()
	app.addView(YMAudioView())
	try: app.run()
	except KeyboardInterrupt as ex: exit(ex)

# by Sdore, 2022
#  www.sdore.me
