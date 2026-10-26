// 网盘目录浏览、115 双凭证认证与阿里云盘单凭证登录
import { apiUrl, del, get, post, put, query } from './request';

export interface IDirItem {
  id: string;
  name: string;
}

export interface IDirListing {
  items: IDirItem[];
  /** 从根目录到当前目录的路径，含根目录本身（id 为 0）。 */
  path: IDirItem[];
  next_offset: number | null;
  via: string;
}

export interface IAuth115Side {
  state: 'unknown' | 'active' | 'expired' | 'error';
  configured: boolean;
  source?: 'file' | 'none';
  error?: string;
  checked_at?: number | null;
  has_access_token?: boolean;
  has_refresh_token?: boolean;
  app_id?: number | null;
  updated_at?: number;
}

export interface IAuth115Traffic {
  cooling_down: boolean;
  cooldown_until?: number | null;
  cooldown_remaining?: number;
  cooldown_reason?: string;
  open_qps?: number;
  cookie_qps?: number;
  share_qps?: number;
}

export interface IAuth115Status {
  cookie: IAuth115Side;
  oauth: IAuth115Side;
  traffic?: IAuth115Traffic;
  ready: boolean;
  writes_via: 'oauth' | 'cookie' | 'none';
}

export interface IQrStart { uid: string; qrcode_url: string; app_id?: number; app?: string; expires_at: number }
export interface IQrStatus { status: number; message: string; authenticated: boolean; state?: string }

/** 两种扫码：cookie 是扫码登录（接收分享），oauth 是文件管理授权（改名整理）。 */
export type QrMode = 'cookie' | 'oauth';

const qrBase = (mode: QrMode) => (mode === 'cookie' ? 'auth115/cookie/qr' : 'auth115/oauth/qr');

export interface IAuthAliyunStatus {
  configured: boolean;
  state: 'not_configured' | 'unknown' | 'active' | 'expired' | 'error';
  checked_at: number | null;
  drive_id: string | null;
  error: string | null;
}

export interface IAliyunQrStart { uid: string; expires_at: number }
export interface IAliyunQrStatus {
  status: 'NEW' | 'SCANED' | 'CONFIRMED' | 'EXPIRED' | 'CANCELED';
  message: string;
  authenticated: boolean;
}

export const storageApi = {
  dirs: (cid: string, offset = 0) => get<IDirListing>(`storage/dirs${query({ cid, offset })}`),
  auth115Status: () => get<IAuth115Status>('auth115/status'),
  auth115Ping: () => post<{ cookie: string; oauth: string; status: IAuth115Status }>('auth115/ping'),
  auth115SetCookie: (cookies: string) => put<{ ok: boolean; state: string }>('auth115/cookie', { cookies }),
  qrStart: (mode: QrMode) => post<IQrStart>(qrBase(mode)),
  qrStatus: (mode: QrMode, uid: string) => get<IQrStatus>(`${qrBase(mode)}${query({ uid })}`),
  qrImageUrl: (mode: QrMode, uid: string) => apiUrl(`${qrBase(mode)}/image${query({ uid })}`),
  auth115ClearOauth: () => del<{ ok: boolean }>('auth115/oauth'),
  authAliyunStatus: () => get<IAuthAliyunStatus>('authaliyun/status'),
  authAliyunPing: () => post<IAuthAliyunStatus>('authaliyun/ping'),
  authAliyunSetToken: (refresh_token: string) =>
    put<{ ok: boolean; status: IAuthAliyunStatus }>('authaliyun/token', { refresh_token }),
  authAliyunClearToken: () => del<{ ok: boolean; status: IAuthAliyunStatus }>('authaliyun/token'),
  authAliyunQrStart: () => post<IAliyunQrStart>('authaliyun/qr'),
  authAliyunQrStatus: (uid: string) => get<IAliyunQrStatus>(`authaliyun/qr${query({ uid })}`),
  authAliyunQrImageUrl: (uid: string) => apiUrl(`authaliyun/qr/image${query({ uid })}`),
  authAliyunQrCancel: (uid: string) => del<{ ok: boolean }>(`authaliyun/qr${query({ uid })}`),
};
