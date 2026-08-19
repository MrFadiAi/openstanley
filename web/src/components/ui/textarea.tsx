import * as React from 'react';
import { cn } from '@/lib/utils';

const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      'w-full rounded-lg border border-edge bg-panel2 px-3 py-2 text-[13.5px] leading-relaxed text-base placeholder:text-muted/70 focus-visible:border-accent/70 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-45',
      className,
    )}
    {...props}
  />
));
Textarea.displayName = 'Textarea';

export { Textarea };
