import { useState } from 'react';
import { Button, Input, Popconfirm, Tag, message } from 'antd';
import { KeyOutlined } from '@ant-design/icons';

import { clearSitePassword, setSitePassword, type SiteItem } from '../../api';
import { t } from '../../i18n';

/** 访问密码是可见性之外的一道闸门，单独成签，让列表里一眼看出哪些站点要验证。 */
export function SitePasswordTag({ site }: { site: SiteItem }) {
  if (!site.has_password) return null;
  return <Tag icon={<KeyOutlined />}>{t('密码')}</Tag>;
}

/**
 * 站点访问密码的统一管理单元：设置 / 修改 / 停用都在这一处完成。
 * 访客侧的验证页由后端直出（api/routes/site_gate.py），所有加密站点共用同一个页面。
 * 密码规则只由后端把关，这里不再重复一份长度判断，出错直接显示后端给的说明。
 */
export function SitePasswordField({
  site,
  onChanged,
}: {
  site: SiteItem;
  onChanged: (updated: SiteItem) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  const close = () => {
    setEditing(false);
    setValue('');
  };

  const handleSave = async () => {
    setBusy(true);
    try {
      onChanged(await setSitePassword(site.site_id, value.trim(), site.origin));
      message.success(t('访问密码已生效'));
      close();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleClear = async () => {
    setBusy(true);
    try {
      onChanged(await clearSitePassword(site.site_id, site.origin));
      message.success(t('访问密码已关闭'));
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <div className="jx-sites-passwordRow">
        <Input.Password
          autoFocus
          value={value}
          maxLength={128}
          onChange={(e) => setValue(e.target.value)}
          onPressEnter={handleSave}
          placeholder={t('至少 4 位')}
        />
        <Button type="primary" loading={busy} onClick={handleSave}>{t('保存')}</Button>
        <Button onClick={close}>{t('取消')}</Button>
      </div>
    );
  }

  return (
    <div className="jx-sites-passwordRow">
      <span className="jx-sites-passwordState">
        {site.has_password ? t('已开启') : t('未设置')}
      </span>
      <Button size="small" onClick={() => setEditing(true)}>
        {site.has_password ? t('修改密码') : t('设置密码')}
      </Button>
      {site.has_password ? (
        <Popconfirm
          title={t('关闭后任何人凭链接即可访问，确定关闭？')}
          okText={t('关闭密码')}
          okButtonProps={{ danger: true }}
          cancelText={t('取消')}
          onConfirm={handleClear}
        >
          <Button size="small" danger loading={busy}>{t('关闭密码')}</Button>
        </Popconfirm>
      ) : null}
    </div>
  );
}
