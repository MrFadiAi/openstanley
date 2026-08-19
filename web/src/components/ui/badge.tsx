import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 whitespace-nowrap',
  {
    variants: {
      variant: {
        default: 'border-edge bg-panel2 text-muted',
        accent: 'border-accent/40 bg-accent/15 text-accent2',
        green: 'border-good/40 bg-good/15 text-good',
        amber: 'border-warn/40 bg-warn/15 text-warn',
        red: 'border-bad/40 bg-bad/15 text-bad',
        cyan: 'border-cyan/40 bg-cyan/15 text-cyan',
      },
    },
    defaultVariants: { variant: 'default' },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
