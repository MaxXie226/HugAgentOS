import type { ReactNode } from 'react';
import { DownloadOutlined, LoadingOutlined } from '@ant-design/icons';
import type { DesktopUpdateStatus } from './useDesktopUpdateStatus';
import { desktopMenuActionUrl } from './desktopBridge';
import { t } from '../i18n';

export function DesktopUpdateEntry({ status, className, children }: {
  status: DesktopUpdateStatus | null;
  className: string;
  children: ReactNode;
}) {
  if (!status?.available_version) return <>{children}</>;
  const label = t('下载更新') + ' · ' + status.available_version;
  return (
    <button type="button" className={className} title={label} aria-label={label}
      disabled={status.busy}
      onClick={() => { window.location.href = desktopMenuActionUrl('check_update'); }}>
      {status.busy ? <LoadingOutlined spin /> : <DownloadOutlined />}
    </button>
  );
}
