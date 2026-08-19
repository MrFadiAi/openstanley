import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'inline-flex select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-lg text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:pointer-events-none disabled:opacity-45',
  {
    variants: {
      variant: {
        default: 'cursor-pointer border border-edge bg-panel2 text-base hover:border-accent/60 hover:text-base',
        primary:
          'cursor-pointer border border-accent bg-accent text-white hover:bg-accent/85 hover:border-accent/85',
        green:
          'cursor-pointer border border-good bg-good text-white hover:bg-good/85 hover:border-good/85',
        danger:
          'cursor-pointer border border-bad/50 bg-transparent text-bad hover:border-bad hover:bg-bad/10',
        ghost: 'cursor-pointer border border-transparent text-muted hover:bg-panel2 hover:text-base',
      },
      size: {
        default: 'h-8 px-3.5',
        sm: 'h-7 px-2.5 text-xs',
        lg: 'h-9 px-5',
        icon: 'h-8 w-8 p-0',
        'icon-sm': 'h-7 w-7 p-0',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button';
    return <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />;
  },
);
Button.displayName = 'Button';

export { Button, buttonVariants };
