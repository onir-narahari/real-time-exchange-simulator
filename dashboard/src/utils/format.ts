export function formatPrice(value: number | null | undefined): string {
  if (value == null) return '—';
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatQty(value: number | null | undefined): string {
  if (value == null) return '—';
  return value.toLocaleString();
}

export function formatPct(value: number | null | undefined): string {
  if (value == null) return '—';
  return `${value.toFixed(2)}%`;
}

export function formatTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function formatClock(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}
