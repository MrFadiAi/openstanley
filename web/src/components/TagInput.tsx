import { useState } from 'react';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';

interface TagInputProps {
  tags: string[];
  onChange: (tags: string[]) => void;
  placeholder: string;
  /** optional prefix applied to entered values, e.g. "@" */
  prefix?: string;
  className?: string;
}

/** Tag-list editor: Enter adds, X removes. */
export function TagInput({ tags, onChange, placeholder, prefix, className }: TagInputProps) {
  const [value, setValue] = useState('');

  const add = (): void => {
    const v = value.trim().replace(/^@/, '');
    if (!v) return;
    if (!tags.includes(v)) onChange([...tags, v]);
    setValue('');
  };

  return (
    <div className={cn('flex flex-wrap items-center gap-1.5', className)}>
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex items-center gap-1 rounded-full border border-edge bg-panel2 px-2.5 py-0.5 text-[12px]"
        >
          {prefix}
          {tag}
          <button
            type="button"
            onClick={() => onChange(tags.filter((x) => x !== tag))}
            className="cursor-pointer text-muted hover:text-bad"
            aria-label={`remove ${tag}`}
          >
            <X size={11} />
          </button>
        </span>
      ))}
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            add();
          }
        }}
        onBlur={add}
        placeholder={placeholder}
        className="h-7 min-w-[180px] flex-1 rounded-lg border border-edge bg-panel2 px-2.5 text-[12.5px] text-base placeholder:text-muted/70 focus:border-accent/70 focus:outline-none"
      />
    </div>
  );
}
