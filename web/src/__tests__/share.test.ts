import { describe, expect, it } from 'vitest';
import { shareLinkStatus, shareProviderName } from '@/lib/share';

describe('测试识别的分享网盘标签', () => {
  it('按确定性识别的网盘 Provider 展示，不再写死 115', () => {
    expect(shareLinkStatus(true, 'aliyun')).toBe('已提取阿里云盘链接');
    expect(shareLinkStatus(true, 'p115')).toBe('已提取115 网盘链接');
    expect(shareProviderName('aliyun')).toBe('阿里云盘');
    expect(shareLinkStatus(false, 'aliyun')).toBe('没有找到支持的网盘分享链接');
    expect(shareLinkStatus(true, null)).toBe('已提取网盘链接');
  });
});
