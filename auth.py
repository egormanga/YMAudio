#!/usr/bin/env python3
# Yandex Music auth

from __future__ import annotations

import os, abc, sys, requests

class YMAuth(abc.ABC):
	@abc.abstractmethod
	def get_token(self, login: str, password: str, captcha: YMAuthCaptcha = None) -> str -- access_token: ...

class YMAuthMobile(YMAuth):  # based on: https://github.com/MarshalX/yandex-music-token
	CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
	CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"
	#CLIENT_ID = "f8cab64f154b4c8e96f92dac8becfcaa"
	#CLIENT_SECRET = "5dd2389483934f02bd51eaa749add5b2"
	X_TOKEN_CLIENT_ID = "c0ebe342af7d48fbbbfcf2d2eedb8f9e"
	X_TOKEN_CLIENT_SECRET = "ad0a908f0aa341a182a37ecd75bc319e"

	base_url = "https://mobileproxy.passport.yandex.net"
	sdk_params = "app_id=ru.yandex.mobile.music&app_version_name=5.08&manufacturer=Apple&device_name=iPhone&app_platform=iPhone&model=iPhone12,1"
	user_agent = "com.yandex.mobile.auth.sdk/5.151.60676 (Apple iPhone12,1; iOS 14.1)"

	def get_token(self, login: str, password: str, captcha: YMAuthCaptcha = None) -> str -- access_token:
		s = self._get_session()
		track_id = self.start_authentication(s, login=login)
		x_token = self.send_authentication_password(s, track_id=track_id, password=password, captcha=captcha)
		access_token = self.generate_yandex_music_token_by_x_token(s, x_token=x_token)
		return access_token

	def _get_session(self) -> requests.Session:
		s = requests.Session()
		s.get(f"{self.base_url}/1/bundle/suggest/mobile_language/?language=ru", headers={'User-Agent': self.user_agent, 'Ya-Client-Accept-Language': 'ru'})
		s.get(f"{self.base_url}/1/bundle/experiments/by_device_id/?{self.sdk_params}")
		return s

	def start_authentication(self, s: requests.Session, login: str, *, lang: str = 'ru') -> str -- track_id:
		r = s.post(f"{self.base_url}/2/bundle/mobile/start/?{self.sdk_params}", data={
			'client_id': self.CLIENT_ID,
			'client_secret': self.CLIENT_SECRET,
			'x_token_client_id': self.X_TOKEN_CLIENT_ID,
			'x_token_client_secret': self.X_TOKEN_CLIENT_SECRET,
			'payment_auth_retpath': 'yandexmusic://am/payment_auth',
			#'force_register': 'false',
			#'is_phone_number': 'false',
			'login': login,
			'display_language': lang,
		}, headers={'User-Agent': self.user_agent}).json()

		if (r.get('status') == 'ok'): return r['track_id']

		error_description = r.get('error_description')
		for error in r['errors']:
			raise YMAuthError(error, error_description)

	def send_authentication_password(self, s: requests.Session, track_id: str, password: str, captcha: YMAuthCaptcha = None) -> str -- x_token:
		r = s.post(f"{self.base_url}/1/bundle/mobile/auth/password/", data={
			'password_source': 'Login',
			'track_id': track_id,
			'password': password,
			**({'captcha_answer': captcha.answer} if (captcha is not None) else {}),
		}, headers={'User-Agent': self.user_agent}).json()

		if (r['status'] == 'ok'): return r['x_token']

		error_description = r.get('error_description')
		for error in r['errors']:
			match error:
				case 'password.not_matched': raise YMAuthWrongPassword(error, error_description, track_id=track_id)
				case 'captcha.required': raise YMAuthCaptcha(error, error_description, track_id=track_id, image_url=r['captcha_image_url'])
				case 'captcha.not_shown': raise YMAuthCaptchaNotShown(error, error_description, track_id=track_id)
				case _: raise YMAuthError(error, error_description, track_id=track_id)

	def generate_yandex_music_token_by_x_token(self, s: requests.Session, x_token: str) -> str -- access_token:
		r = s.post(f"{self.base_url}/1/token/?{self.sdk_params}", data={
			'access_token': x_token,
			'client_id': self.CLIENT_ID,
			'client_secret': self.CLIENT_SECRET,
			'grant_type': 'x-token',
		}, headers={'User-Agent': self.user_agent}).json()

		try: return r['access_token']
		except KeyError: pass

		error_description = r.get('error_description')
		for error in r['errors']:
			raise YMAuthError(error, error_description)

class YMAuthWeb(YMAuth):
	CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
	CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"

	base_url = "https://oauth.yandex.ru"
	user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36"

	def get_token(self, login: str, password: str, captcha: YMAuthCaptcha = None, *, lang: str = 'ru') -> str -- access_token:
		r = requests.post(f"{self.base_url}/token", data={
			'grant_type': 'password',
			'client_id': self.CLIENT_ID,
			'client_secret': self.CLIENT_SECRET,
			'username': login,
			'password': password,
			**({'x_captcha_key': captcha.key, 'x_captcha_answer': captcha.answer} if (captcha is not None) else {}),
		}, headers={'User-Agent': self.user_agent}).json()

		try: return r['access_token']
		except KeyError: pass

		error = r['error']
		error_description = r.get('error_description')
		if (error == 'invalid_grant'): raise YMAuthWrongPassword(error, error_description)
		elif (error == '403' and 'x_captcha_key' in r): raise YMAuthCaptcha(error, error_description, key=r['x_captcha_key'], image_url=r['x_captcha_url'])
		else: raise YMAuthError(error, error_description)

class YMAuthException(Exception): pass
class YMAuthError(YMAuthException):
	"Authorization error"

	error: str
	error_description: str
	track_id: str

	def __init__(self, error, error_description=None, /, *args, track_id: str = None, **kwargs):
		if (error_description is None): error_description = self.__doc__
		super().__init__(*args, **kwargs)
		self.error, self.error_description, self.track_id = error, error_description, track_id

	def __str__(self):
		return self.error_description
class YMAuthWrongPassword(YMAuthError): "Incorrect login or password"
class YMAuthCaptcha(YMAuthError):
	"Captcha required"

	key: str
	image_url: str
	answer: str

	def __init__(self, *args, key: str = None, image_url: str, answer: str = None, **kwargs):
		super().__init__(*args, **kwargs)
		self.key, self.image_url, self.answer = key, image_url, answer

	def __str__(self):
		if (sys.stderr.isatty() and hasattr(sys, 'ps1')):
			try: from cimg import Image, showimg
			except ImportError: pass
			else:
				w, h = os.get_terminal_size()
				return '\n' + showimg(self.image_url, (w, h-1), double_vres=True, resample=Image.Resampling.BICUBIC)
		return super().__str__()

	def with_answer(self, answer: str) -> YMAuthCaptcha:
		return self.__class__(self.error, self.error_description, *self.args, track_id=self.track_id, key=self.key, image_url=self.image_url, answer=str(answer))
class YMAuthCaptchaNotShown(YMAuthError): "The captcha hasn't been shown"

# by Sdore, 2022
#  www.sdore.me
