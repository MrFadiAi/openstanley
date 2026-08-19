import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

// Apply stored language / density before first paint.
const lang = localStorage.getItem('xs.lang') === 'ar' ? 'ar' : 'en';
document.documentElement.lang = lang;
document.documentElement.dir = lang === 'ar' ? 'rtl' : 'ltr';
if (localStorage.getItem('xs.dense') === '1') {
  document.documentElement.classList.add('dense');
}

const el = document.getElementById('root');
if (el) {
  ReactDOM.createRoot(el).render(<App />);
}
