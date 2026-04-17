import express from 'express';
import { authRouter } from './routes/auth.js';
import { protectedRouter } from './routes/protected.js';

const app = express();
app.use(express.json());

app.use('/auth', authRouter);
app.use('/api', protectedRouter);

app.listen(3000, () => console.log('Server on port 3000'));
