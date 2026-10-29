import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { STATE_META, connectionMeta, llmErrorExplanation } from '@/lib/status';

describe('任务状态文案与后端状态机保持一致', () => {
  it('后端 TaskState 的每个取值都有界面文案', () => {
    // 直接读取后端契约，后端新增状态而前端遗漏时这里会失败。
    const contracts = readFileSync(resolve(import.meta.dirname, '../../../reelay/core/contracts.py'), 'utf8');
    const block = contracts.slice(contracts.indexOf('class TaskState'), contracts.indexOf('class ApprovalMode'));
    const backendStates = [...block.matchAll(/^\s+[A-Z_]+ = "([a-z_]+)"/gm)].map((match) => match[1]);
    expect(backendStates.length).toBeGreaterThan(10);
    expect(Object.keys(STATE_META).sort()).toEqual(backendStates.sort());
  });
});

describe('AI status wording does not confuse incomplete metadata with connectivity failure', () => {
  it('distinguishes a schema-valid incomplete extraction from a broken API', () => {
    expect(connectionMeta('incomplete', 'llm')).toEqual({
      label: '最近识别信息不足', tone: 'warning',
    });
    expect(connectionMeta('connected', 'llm').tone).toBe('success');
    expect(connectionMeta('failed', 'llm').label).toBe('最近识别失败');
    expect(connectionMeta('failed').label).toBe('连接失败');
    expect(llmErrorExplanation('missing_title_or_type')).toContain('接口并未断开');
  });
});
