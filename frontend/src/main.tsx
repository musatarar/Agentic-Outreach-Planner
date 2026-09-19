import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { RequireAuth } from './components/RequireAuth';
import { ActionsPage } from './pages/ActionsPage';
import { ConsumePage } from './pages/ConsumePage';
import { InboxPage } from './pages/InboxPage';
import { LeadsPage } from './pages/LeadsPage';
import { SignInPage } from './pages/SignInPage';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('#root element not found');

// Every route here needs a Django shell in project/urls.py, and vice versa.
createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/signin" element={<SignInPage />} />
        <Route path="/auth/consume" element={<ConsumePage />} />
        <Route path="/leads/" element={<RequireAuth><LeadsPage /></RequireAuth>} />
        <Route path="/actions" element={<RequireAuth><ActionsPage /></RequireAuth>} />
        <Route path="/inbox" element={<RequireAuth><InboxPage /></RequireAuth>} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
