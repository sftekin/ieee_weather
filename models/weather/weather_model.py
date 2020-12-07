import torch
import torch.nn as nn
import torch.nn.functional as F

from models.weather.attention import Attention
from models.weather.f_conv_lstm import FConvLSTMCell
from models.weather.input_cnn import InputCNN


class WeatherModel(nn.Module):
    def __init__(self, window_in, window_out, input_size, num_series,
                 selected_dim, encoder_params, decoder_params, device):
        super().__init__()

        self.height, self.width = input_size
        self.selected_dim = selected_dim
        self.window_in = window_in
        self.window_out = window_out
        self.num_series = num_series

        self.encoder_params = encoder_params
        self.decoder_params = decoder_params
        self.device = device
        self.is_trainable = True

        self.input_cnn = InputCNN(in_channels=self.window_in)

        self.encoder = FConvLSTMCell(input_size=(self.height, self.width),
                                     input_dim=self.num_series,
                                     hidden_dim=encoder_params['hidden_dim'],
                                     flow_dim=encoder_params['flow_dim'],
                                     kernel_size=encoder_params['kernel_size'],
                                     bias=encoder_params['bias'],
                                     padding=encoder_params['padding'],
                                     device=self.device)

        self.input_attn = Attention(input_size=encoder_params['attn_input_size'],
                                    hidden_size=(self.height, self.width),
                                    input_dim=encoder_params['attn_input_dim'],
                                    hidden_dim=encoder_params['hidden_dim'],
                                    attn_dim=encoder_params['attn_dim'])

        self.decoder = FConvLSTMCell(input_size=(self.height, self.width),
                                     input_dim=decoder_params['input_dim'],
                                     hidden_dim=encoder_params['hidden_dim'],
                                     flow_dim=decoder_params['flow_dim'],
                                     kernel_size=decoder_params['kernel_size'],
                                     bias=decoder_params['bias'],
                                     padding=decoder_params['padding'],
                                     device=device)

        self.out_conv = nn.Conv2d(in_channels=encoder_params['hidden_dim'],
                                  out_channels=1,
                                  kernel_size=3,
                                  padding=1,
                                  bias=False)

        self.output_attn = Attention(input_size=(self.height, self.width),
                                     hidden_size=(self.height, self.width),
                                     input_dim=encoder_params['hidden_dim'],
                                     hidden_dim=decoder_params['hidden_dim'],
                                     attn_dim=decoder_params['attn_dim'])
        self.hidden = None

    def init_hidden(self, batch_size):
        hidden = self.encoder.init_hidden(batch_size)
        return hidden

    def forward(self, x, f_x, hidden):
        """

        :param x: (b, t, d, m, n)
        :type x:
        :param f_x: (b, t, 4, m, n)
        :type f_x:
        :param hidden: [(b, d', m, n), (b, d', m, n)]
        :type hidden:
        :return:
        :rtype:
        """
        batch_size, win_len, dim_len, height, width = x.shape

        # calculate input attention
        alpha_list = []
        for k in range(dim_len):
            # dim(x_k): (b, 256, m', n')
            x_k = self.input_cnn(x[:, :, k])

            # dim(alpha): (B, 1)
            alpha = self.input_attn(x_k, hidden)
            alpha_list.append(alpha)

        # dim(alpha_tensor): (B, D)
        alpha_tensor = torch.cat(alpha_list, dim=1).squeeze()
        alpha_tensor = F.softmax(alpha_tensor, dim=1)

        # calculate encoder output
        en_out = []
        for t in range(self.window_in):
            x_t = x[:, t].view(batch_size, dim_len, -1)
            x_tilda = x_t * alpha_tensor.unsqueeze(2)
            x_tilda = x_tilda.view(batch_size, dim_len, height, width)

            hidden = self.encoder(input_tensor=x_tilda,
                                  cur_state=hidden,
                                  flow_tensor=f_x[:, t])

            en_out.append(hidden[0])
        en_out = torch.stack(en_out, dim=1)

        de_hidden = self.decoder.init_hidden(batch_size)
        de_in = x[:, -1, [self.selected_dim]]
        f_y = f_x[:, -1]
        de_out = []
        # parse decoder layer and get outputs recursively
        for t in range(self.window_out):
            beta_list = []
            for k in range(self.window_in):
                beta = self.output_attn(en_out[:, k], de_hidden)
                beta_list.append(beta)
            # dim(beta_tensor): (B, T)
            beta_tensor = F.softmax(torch.cat(beta_list, dim=1).squeeze(), dim=1)

            en_dim = en_out.shape[2]
            context_t = torch.sum(en_out.view(batch_size, self.window_in, -1) * beta_tensor.unsqueeze(2), dim=1)
            context_t = context_t.view(batch_size, en_dim, self.decoder.height, self.decoder.width)

            de_hidden = self.decoder(torch.cat([context_t, de_in], dim=1), de_hidden, f_y)
            conv_out = self.out_conv(de_hidden[0])

            f_y = self.create_flow_mat(conv_out, de_in)
            de_out.append(conv_out)
            de_in = conv_out

        de_out = torch.stack(de_out, dim=1)

        return de_out

    @staticmethod
    def create_flow_mat(y, y_prev):
        batch_dim, d_dim, height, width = y.shape
        y_t = y[..., 1:height - 1, 1:width - 1]
        f_a = y_t - y_prev[..., :height - 2, :width - 2]
        f_b = y_t - y_prev[..., 2:height, 2:width]
        f_c = y_t - y_prev[..., 2:height, :width - 2]
        f_d = y_t - y_prev[..., :height - 2, 2:width]
        f = torch.cat([f_a, f_b, f_c, f_d], dim=1)
        return f