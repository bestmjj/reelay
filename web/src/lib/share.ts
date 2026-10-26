/** Labels for share hosts detected by the deterministic backend parser. */
export function shareProviderName(value: unknown): string {
  if (value === 'aliyun') return '阿里云盘';
  if (value === 'p115') return '115 网盘';
  return '网盘';
}

export function shareLinkStatus(hasLink: boolean, provider: unknown): string {
  const cloud = shareProviderName(provider);
  return hasLink ? `已提取${cloud}链接` : '没有找到支持的网盘分享链接';
}
